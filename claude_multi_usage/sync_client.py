"""Building and posting sync payloads to the server.

Shared by ``cmu sync``, ``cmu autosync run`` and ``cmu import-claude-export``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from .config import get_alias, get_keys, get_server_url
from .parser import load_usage_data, parse_today_hourly


class SyncError(Exception):
    """A sync could not be performed; the message is meant for the user."""


def require_server_config() -> tuple[str, list[dict]]:
    """Return (server_url, keys) or raise SyncError with a hint."""
    server_url = get_server_url()
    if not server_url:
        raise SyncError("Server URL not configured.\nRun: cmu config server <url>")
    keys = get_keys()
    if not keys:
        raise SyncError("No keys configured.\nRun: cmu config key add <your-key>")
    return server_url, keys


def build_sync_payload(key_values: list[str]) -> dict:
    """The payload ``cmu sync`` sends for this machine."""
    data = load_usage_data()
    today_hourly = parse_today_hourly()
    now = datetime.now()
    return {
        "hostname": data.hostname,
        "alias": get_alias(),
        "keys": list(key_values),
        "synced_at": now.isoformat(),
        # Cumulative figures from stats-cache.json. They are a few hundred
        # bytes and the diff view's Summary / Model Usage panels need them.
        "total_sessions": data.total_sessions,
        "total_messages": data.total_messages,
        "first_session_date": data.first_session_date,
        "model_usage": [
            {"model": m.model, "input_tokens": m.input_tokens,
             "output_tokens": m.output_tokens, "cache_read_tokens": m.cache_read_tokens,
             "cache_creation_tokens": m.cache_creation_tokens}
            for m in data.model_usage
        ],
        "hour_counts": {str(k): v for k, v in data.hour_counts.items()},
        # Which day today_hourly describes, so stale data is labelled correctly
        "today_hourly_date": now.strftime("%Y-%m-%d"),
        "today_hourly": [
            {"hour": h.hour, "message_count": h.message_count,
             "session_count": h.session_count, "tokens": h.tokens}
            for h in today_hourly
        ],
        "daily_activity": [
            {"date": a.date, "message_count": a.message_count,
             "session_count": a.session_count, "tool_call_count": a.tool_call_count}
            for a in data.daily_activity
        ],
        "daily_model_tokens": [
            {"date": t.date, "tokens_by_model": t.tokens_by_model}
            for t in data.daily_model_tokens
        ],
        "projects": [
            {"name": p.name, "session_count": p.session_count,
             "output_tokens": p.output_tokens, "input_tokens": p.input_tokens,
             "first_seen": p.first_seen.isoformat() if p.first_seen else None,
             "last_seen": p.last_seen.isoformat() if p.last_seen else None}
            for p in data.projects
        ],
    }


def post_payload(server_url: str, payload: dict) -> dict:
    """POST a payload to ``<server_url>/api/sync``; raise SyncError on failure."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/api/sync",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "cmu"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:
            pass
        raise SyncError(f"Sync failed: {e}{' - ' + detail if detail else ''}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SyncError(f"Sync failed: {e}") from e


def sync_now() -> dict:
    """Full sync of this machine. Returns the payload sent; raises SyncError."""
    server_url, keys = require_server_config()
    payload = build_sync_payload([k["key"] for k in keys])
    post_payload(server_url, payload)
    return payload
