"""Incremental cost cache - past days are fixed, only today is realtime."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .pricing import calculate_model_cost, get_pricing
from .parser import _utc_to_local

CACHE_DIR = Path.home() / ".claude-multi-usage"
CACHE_FILE = CACHE_DIR / "cost-cache.json"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _parse_sessions_for_date_range(start_date: str, end_date: str):
    """Parse session jsonl files and aggregate costs by date+model.

    Returns dict: { "2026-03-06": { "claude-opus-4-6": { cost, output, input, cache_read, cache_create } } }
    """
    daily_costs = defaultdict(lambda: defaultdict(lambda: {
        "cost": 0.0, "output_tokens": 0, "input_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }))

    if not CLAUDE_PROJECTS_DIR.exists():
        return daily_costs

    seen_msg_ids = set()

    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("**/*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime).strftime("%Y-%m-%d")
                if mtime < start_date:
                    continue
            except OSError:
                continue

            try:
                with open(session_file) as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # "assistant" entries: direct responses
                        # "progress" entries: subagent (e.g. haiku) responses
                        entry_type = d.get("type")
                        if entry_type == "assistant":
                            ts = d.get("timestamp", "")
                            msg = d.get("message", {})
                        elif entry_type == "progress":
                            data = d.get("data", {})
                            if not isinstance(data, dict):
                                continue
                            inner = data.get("message", {})
                            if not isinstance(inner, dict):
                                continue
                            msg = inner.get("message", {})
                            if not isinstance(msg, dict):
                                continue
                            ts = inner.get("timestamp", "")
                        else:
                            continue

                        # Convert the UTC timestamp to local time
                        local_dt = _utc_to_local(ts)
                        date_str = local_dt.strftime("%Y-%m-%d") if local_dt else ""
                        if not date_str or date_str < start_date or date_str > end_date:
                            continue

                        model = msg.get("model", "")
                        if not model or "claude" not in model:
                            continue

                        # Skip duplicate messages (each message ID is counted once)
                        msg_id = msg.get("id", "")
                        if msg_id:
                            if msg_id in seen_msg_ids:
                                continue
                            seen_msg_ids.add(msg_id)

                        usage = msg.get("usage", {})

                        out = usage.get("output_tokens", 0)
                        inp = usage.get("input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)

                        entry = daily_costs[date_str][model]
                        entry["output_tokens"] += out
                        entry["input_tokens"] += inp
                        entry["cache_read_tokens"] += cache_read
                        entry["cache_creation_tokens"] += cache_create
                        entry["cost"] += calculate_model_cost(
                            model, inp, out, cache_read, cache_create
                        )
            except (OSError, IOError):
                continue

    return daily_costs


def load_cost_cache() -> dict:
    """Load existing cost cache or return empty."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_cached_date": None, "daily_costs": {}, "total_cost": 0.0}


def save_cost_cache(cache: dict) -> None:
    """Save cost cache to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def get_costs() -> Optional[tuple]:
    """Get accurate costs using incremental cache.

    Returns (daily_costs_dict, total_cost, today_cost) or None if pricing unavailable.
    """
    if get_pricing() is None:
        return None
    cache = load_cost_cache()
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    last_cached = cache.get("last_cached_date")
    cached_daily = cache.get("daily_costs", {})
    needs_save = False

    # No cache yet: parse everything (first run)
    if last_cached is None:
        start = "2020-01-01"
    elif last_cached < yesterday_str:
        # Parse from the day after the cached date up to yesterday
        start = (datetime.strptime(last_cached, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start = None  # cache is current; no historical parsing needed

    # Parse historical data (up to yesterday)
    if start:
        new_daily = _parse_sessions_for_date_range(start, yesterday_str)
        for date_str, models in new_daily.items():
            # Store as plain dicts
            cached_daily[date_str] = {
                model: {
                    "cost": data["cost"],
                    "output_tokens": data["output_tokens"],
                    "input_tokens": data["input_tokens"],
                    "cache_read_tokens": data["cache_read_tokens"],
                    "cache_creation_tokens": data["cache_creation_tokens"],
                }
                for model, data in models.items()
            }
        cache["last_cached_date"] = yesterday_str
        cache["daily_costs"] = cached_daily
        cache["total_cost"] = sum(
            data["cost"]
            for models in cached_daily.values()
            for data in models.values()
        )
        needs_save = True

    # Today's data is computed in realtime (never cached)
    today_daily = _parse_sessions_for_date_range(today_str, today_str)
    today_cost = sum(
        data["cost"]
        for models in today_daily.values()
        for data in models.values()
    )

    if needs_save:
        save_cost_cache(cache)

    total_cost = cache.get("total_cost", 0.0) + today_cost

    # Return daily_costs including today's data
    all_daily = dict(cached_daily)
    for date_str, models in today_daily.items():
        all_daily[date_str] = {
            model: {
                "cost": data["cost"],
                "output_tokens": data["output_tokens"],
            }
            for model, data in models.items()
        }

    return all_daily, total_cost, today_cost
