"""Import a claude.ai account data export as a pseudo-device.

The export (claude.ai: Settings > Privacy > Export data, web or desktop app)
is a ZIP with ``conversations.json``, ``users.json``, ``projects.json`` and
``memories.json``. It is account-wide, so it covers chats from the web,
desktop, Android and iOS apps alike.

It carries message text and timestamps but no token counts or model names,
so tokens are *estimated* from text length (about four characters per
token) and reported under the model name ``claude.ai (estimated)``.
Message and conversation counts and dates are exact.

Each account becomes its own device, ``claude.ai-<8 hex chars of the
account id>``. An export is a complete snapshot of the account, so the
payload is marked ``snapshot`` and the server replaces the device's data
instead of merging: the most recent import wins, whatever it contains.

Anthropic offers no API or schedule for the export; it is requested by
hand and arrives as an emailed link.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

EXPORT_HOSTNAME_PREFIX = "claude.ai"
DEFAULT_ALIAS = "claude.ai"
ESTIMATED_MODEL = "claude.ai (estimated)"
ESTIMATED_SUFFIX = "(estimated)"
CHARS_PER_TOKEN = 4
UNGROUPED_PROJECT = "claude.ai chats"
# The flattened `text` of a message stands in for non-text blocks with this.
PLACEHOLDER = "This block is not supported on your current device yet."
# conversations.json is loaded into memory as a whole; refuse absurd sizes.
MAX_EXPORT_BYTES = 1 << 30

_SIBLINGS = ("projects.json", "users.json")


class ExportError(ValueError):
    pass


# -- loading --------------------------------------------------------------------

def load_export(path: Path | str) -> dict[str, Any]:
    """Return {"conversations": [...], "projects": [...], "users": [...]}.

    ``path`` is the export ZIP or a directory it was extracted into. The
    JSON files may sit at the top level or in a subdirectory; projects.json
    and users.json are read from the folder that holds conversations.json.
    """
    path = Path(path)
    try:
        if path.is_dir():
            return _load_from_dir(path)
        if zipfile.is_zipfile(path):
            return _load_from_zip(path)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError) as e:
        raise ExportError(f"cannot read {path}: {e}") from e
    raise ExportError(f"{path} is neither a ZIP file nor a directory")


def _load_from_dir(path: Path) -> dict[str, Any]:
    found = sorted(path.rglob("conversations.json"))
    if not found:
        raise ExportError(f"no conversations.json found under {path}")
    if len(found) > 1:
        listing = "\n  ".join(str(p.parent) for p in found)
        raise ExportError(f"several exports found under {path}; point at one of them:\n  {listing}")
    folder = found[0].parent

    def read(name: str) -> Any:
        p = folder / name
        if not p.exists():
            return None
        _check_size(p.stat().st_size, name)
        return _parse(p.read_bytes(), name)

    return _assemble(read("conversations.json"), read("projects.json"), read("users.json"))


def _load_from_zip(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        found = [i for i in zf.infolist() if posixpath.basename(i.filename) == "conversations.json"]
        if not found:
            raise ExportError(f"no conversations.json found in {path}")
        if len(found) > 1:
            listing = "\n  ".join(i.filename for i in found)
            raise ExportError(f"several exports found in {path}; extract and point at one of them:\n  {listing}")
        folder = posixpath.dirname(found[0].filename)
        by_name = {i.filename: i for i in zf.infolist()}

        def read(name: str) -> Any:
            info = by_name.get(posixpath.join(folder, name) if folder else name)
            if info is None:
                return None
            _check_size(info.file_size, name)
            return _parse(zf.read(info), name)

        return _assemble(read("conversations.json"), read("projects.json"), read("users.json"))


def _check_size(size: int, name: str) -> None:
    if size > MAX_EXPORT_BYTES:
        raise ExportError(
            f"{name} is {size / 1e9:.1f} GB; the importer loads it into memory and refuses files "
            f"over {MAX_EXPORT_BYTES / 1e9:.0f} GB"
        )


def _parse(data: bytes, name: str) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, ValueError) as e:
        raise ExportError(f"{name} is not valid JSON: {e}") from e


def _assemble(conversations: Any, projects: Any, users: Any) -> dict[str, Any]:
    if not isinstance(conversations, list) or not all(isinstance(c, dict) for c in conversations):
        raise ExportError("conversations.json does not contain a list of conversations")
    return {
        "conversations": conversations,
        "projects": projects if isinstance(projects, list) else [],
        "users": users if isinstance(users, list) else [],
    }


# -- estimation -----------------------------------------------------------------

def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return _tokens(len(text))


def _tokens(chars: int) -> int:
    return max(1, round(chars / CHARS_PER_TOKEN)) if chars > 0 else 0


def _nested_text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_nested_text_chars(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "thinking"):
            if key in value:
                return _nested_text_chars(value[key])
    return 0


def message_tokens(message: dict) -> tuple[int, int]:
    """Estimated (input_tokens, output_tokens) for one message.

    Prefers the ``content`` blocks (text, voice notes, thinking and tool
    inputs such as artifacts count for the sender; tool results count as
    input). Falls back to the flattened ``text`` with the "block not
    supported" placeholder removed. Attachment text counts as input.
    """
    is_assistant = message.get("sender") == "assistant"
    content = message.get("content")
    own_chars = in_chars = 0
    if isinstance(content, list) and content:
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                in_chars += _nested_text_chars(block.get("content"))
            elif kind == "thinking":
                own_chars += len(block.get("thinking") or block.get("text") or "")
            elif kind == "tool_use":
                if block.get("input") is not None:
                    own_chars += len(json.dumps(block["input"], ensure_ascii=False))
            else:
                own_chars += len(block.get("text") or "")
    else:
        own_chars = len((message.get("text") or "").replace(PLACEHOLDER, ""))
    for att in message.get("attachments") or []:
        if isinstance(att, dict):
            in_chars += len(att.get("extracted_content") or "")
    if is_assistant:
        return _tokens(in_chars), _tokens(own_chars)
    return _tokens(in_chars + own_chars), 0


_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def _local_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    # Python < 3.11 only accepts 3 or 6 fractional digits.
    text = _FRACTION.sub(lambda m: m.group(1).ljust(7, "0"), value.replace("Z", "+00:00"), count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone()


# -- summary --------------------------------------------------------------------

def account_id(export: dict[str, Any]) -> Optional[str]:
    for conv in export["conversations"]:
        acct = conv.get("account")
        if isinstance(acct, dict) and acct.get("uuid"):
            return str(acct["uuid"])
    for user in export.get("users") or []:
        if isinstance(user, dict) and user.get("uuid"):
            return str(user["uuid"])
    return None


def device_hostname(export: dict[str, Any]) -> str:
    """One device per account: 'claude.ai-<8 hex of the account id>'."""
    acct = account_id(export)
    if not acct:
        return EXPORT_HOSTNAME_PREFIX
    return f"{EXPORT_HOSTNAME_PREFIX}-{hashlib.sha256(acct.encode()).hexdigest()[:8]}"


def _project_of(conv: dict, names: dict) -> str:
    link = conv.get("project_uuid")
    if not link and isinstance(conv.get("project"), dict):
        link = conv["project"].get("uuid")
    return names.get(link) or UNGROUPED_PROJECT


def summarize_export(export: dict[str, Any], keys: list[str], alias: str | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Build a sync payload (see ``cmu sync``) for the account's pseudo-device.

    Days are bucketed in the local time zone of the machine running the
    import. The payload is a snapshot: the server replaces the device.
    """
    conversations = export["conversations"]
    project_names = {
        p.get("uuid"): p.get("name") or "(unnamed project)"
        for p in export.get("projects") or [] if isinstance(p, dict) and p.get("uuid")
    }

    day_messages: dict[str, int] = defaultdict(int)
    day_conversations: dict[str, set] = defaultdict(set)
    day_output: dict[str, int] = defaultdict(int)
    hour_counts: dict[int, int] = defaultdict(int)
    total_in = total_out = total_messages = total_sessions = 0
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
            tok_in, tok_out = message_tokens(m)
            total_messages += 1
            day_messages[day] += 1
            day_conversations[day].add(conv_id)
            day_output[day] += tok_out
            conv_in += tok_in
            conv_out += tok_out
            if conv_first is None or when < conv_first:
                conv_first = when
            if conv_last is None or when > conv_last:
                conv_last = when
        if conv_first is None:
            continue
        total_sessions += 1
        total_in += conv_in
        total_out += conv_out
        hour_counts[conv_first.hour] += 1
        if first_seen is None or conv_first < first_seen:
            first_seen = conv_first

        name = _project_of(conv, project_names)
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
        "hostname": device_hostname(export),
        "alias": alias or DEFAULT_ALIAS,
        "keys": list(keys),
        "synced_at": now.isoformat(),
        "snapshot": True,
        "total_sessions": total_sessions,
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
        ("Device", f"{payload['alias']} ({payload['hostname']})"),
        ("Conversations", f"{payload['total_sessions']:,}"),
        ("Messages", f"{payload['total_messages']:,}"),
        ("Date range", f"{days[0]} to {days[-1]}" if days else "-"),
        ("Active days", f"{len(days):,}"),
        ("Est. output tokens", f"{usage['output_tokens']:,}"),
        ("Est. input tokens", f"{usage['input_tokens']:,}"),
        ("Projects", f"{len(payload['projects']):,}"),
    ]
