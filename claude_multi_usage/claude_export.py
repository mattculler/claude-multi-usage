"""Import a claude.ai account data export as the pseudo-device "claude.ai".

The export (claude.ai: Settings > Privacy > Export data, web or desktop app)
is a ZIP with ``conversations.json``, ``users.json``, ``projects.json`` and
``memories.json``. It is account-wide, so it covers chats from the web,
desktop, Android and iOS apps alike.

It carries message text and timestamps but no token counts or model names,
so tokens are *estimated* from text length (about four characters per
token) and reported under the model name ``claude.ai (estimated)``.
Message and conversation counts and dates are exact.

Anthropic offers no API or schedule for the export; it is requested by
hand and arrives as an emailed link. Importing is idempotent: the server
merges the pseudo-device by date, so re-importing a newer export replaces
the days it covers without double counting.
"""

from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

EXPORT_HOSTNAME = "claude.ai"
ESTIMATED_MODEL = "claude.ai (estimated)"
CHARS_PER_TOKEN = 4
UNGROUPED_PROJECT = "claude.ai chats"

_WANTED = ("conversations.json", "projects.json", "users.json")


class ExportError(ValueError):
    pass


def load_export(path: Path | str) -> dict[str, Any]:
    """Return {"conversations": [...], "projects": [...], "users": [...]}.

    ``path`` is the export ZIP or a directory it was extracted into. The
    JSON files may sit at the top level or in a subdirectory.
    """
    path = Path(path)
    found: dict[str, Any] = {}
    if path.is_dir():
        for name in _WANTED:
            for candidate in sorted(path.rglob(name)):
                found[name] = _load_json(candidate.read_bytes(), name)
                break
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                base = Path(info.filename).name
                if base in _WANTED and base not in found:
                    found[base] = _load_json(zf.read(info), base)
    else:
        raise ExportError(f"{path} is neither a ZIP file nor a directory")

    if "conversations.json" not in found:
        raise ExportError(f"no conversations.json found in {path}")
    conversations = found["conversations.json"]
    if not isinstance(conversations, list) or not all(isinstance(c, dict) for c in conversations):
        raise ExportError("conversations.json does not contain a list of conversations")
    return {
        "conversations": conversations,
        "projects": found.get("projects.json") if isinstance(found.get("projects.json"), list) else [],
        "users": found.get("users.json") if isinstance(found.get("users.json"), list) else [],
    }


def _load_json(data: bytes, name: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ExportError(f"{name} is not valid JSON: {e}") from e


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _message_text(message: dict) -> str:
    text = message.get("text")
    if isinstance(text, str) and text:
        return text
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _local_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt  # already local-naive; nothing better to do
    return dt.astimezone()


def summarize_export(export: dict[str, Any], keys: list[str], alias: str | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Build a sync payload (see ``cmu sync``) for the pseudo-device."""
    conversations = export["conversations"]
    project_names = {
        p.get("uuid"): p.get("name") or "(unnamed project)"
        for p in export.get("projects") or [] if isinstance(p, dict) and p.get("uuid")
    }

    day_messages: dict[str, int] = defaultdict(int)
    day_conversations: dict[str, set] = defaultdict(set)
    day_output: dict[str, int] = defaultdict(int)
    hour_counts: dict[int, int] = defaultdict(int)
    total_in = total_out = total_messages = 0
    first_seen: Optional[datetime] = None
    projects: dict[str, dict] = {}

    for conv in conversations:
        conv_id = conv.get("uuid") or id(conv)
        messages = conv.get("chat_messages") or []
        if not isinstance(messages, list):
            continue
        conv_first: Optional[datetime] = None
        conv_last: Optional[datetime] = None
        conv_in = conv_out = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            when = _local_dt(m.get("created_at")) or _local_dt(conv.get("created_at"))
            if when is None:
                continue
            day = when.strftime("%Y-%m-%d")
            tokens = estimate_tokens(_message_text(m))
            total_messages += 1
            day_messages[day] += 1
            day_conversations[day].add(conv_id)
            if m.get("sender") == "assistant":
                day_output[day] += tokens
                conv_out += tokens
            else:
                conv_in += tokens
            if conv_first is None or when < conv_first:
                conv_first = when
            if conv_last is None or when > conv_last:
                conv_last = when
        if conv_first is None:
            continue
        total_in += conv_in
        total_out += conv_out
        hour_counts[conv_first.hour] += 1
        if first_seen is None or conv_first < first_seen:
            first_seen = conv_first

        name = project_names.get(conv.get("project_uuid")) or UNGROUPED_PROJECT
        entry = projects.setdefault(name, {"name": name, "session_count": 0, "output_tokens": 0,
                                           "input_tokens": 0, "first_seen": None, "last_seen": None})
        entry["session_count"] += 1
        entry["output_tokens"] += conv_out
        entry["input_tokens"] += conv_in
        if entry["first_seen"] is None or conv_first < entry["first_seen"]:
            entry["first_seen"] = conv_first
        if entry["last_seen"] is None or conv_last > entry["last_seen"]:
            entry["last_seen"] = conv_last

    for entry in projects.values():
        entry["first_seen"] = entry["first_seen"].isoformat() if entry["first_seen"] else None
        entry["last_seen"] = entry["last_seen"].isoformat() if entry["last_seen"] else None

    now = now or datetime.now()
    return {
        "hostname": EXPORT_HOSTNAME,
        "alias": alias,
        "keys": list(keys),
        "synced_at": now.isoformat(),
        "total_sessions": sum(1 for c in conversations if c.get("chat_messages")),
        "total_messages": total_messages,
        "first_session_date": first_seen.isoformat() if first_seen else None,
        "model_usage": [{
            "model": ESTIMATED_MODEL, "input_tokens": total_in, "output_tokens": total_out,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }] if total_messages else [],
        "hour_counts": {str(h): n for h, n in sorted(hour_counts.items())},
        "today_hourly_date": None,
        "today_hourly": [],
        "daily_activity": [
            {"date": d, "message_count": day_messages[d],
             "session_count": len(day_conversations[d]), "tool_call_count": 0}
            for d in sorted(day_messages)
        ],
        "daily_model_tokens": [
            {"date": d, "tokens_by_model": {ESTIMATED_MODEL: day_output[d]}}
            for d in sorted(day_messages)
        ],
        "projects": sorted(projects.values(), key=lambda p: p["output_tokens"], reverse=True),
    }


def describe(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Human-readable summary rows for a payload built by summarize_export."""
    days = [d["date"] for d in payload["daily_activity"]]
    usage = payload["model_usage"][0] if payload["model_usage"] else {"input_tokens": 0, "output_tokens": 0}
    return [
        ("Conversations", f"{payload['total_sessions']:,}"),
        ("Messages", f"{payload['total_messages']:,}"),
        ("Date range", f"{days[0]} to {days[-1]}" if days else "-"),
        ("Active days", f"{len(days):,}"),
        ("Est. output tokens", f"{usage['output_tokens']:,}"),
        ("Est. input tokens", f"{usage['input_tokens']:,}"),
        ("Projects", f"{len(payload['projects']):,}"),
    ]
