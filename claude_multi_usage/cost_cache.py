"""Cost figures derived from the session index.

The index stores token counts per message; prices are applied here, at read
time, with whatever pricing table is current. There is therefore nothing to
invalidate when prices change. ``cmu cost --rebuild`` re-reads the session
files themselves (for example after a time zone change).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional

from .index import get_index
from .pricing import get_pricing, is_claude_model, usage_cost


def daily_costs(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Return {date: {model: {cost, output_tokens, input_tokens, cache_read_tokens,
    cache_creation_tokens}}} for Claude models, with every message counted once."""
    result: dict = defaultdict(lambda: defaultdict(lambda: {
        "cost": 0.0, "output_tokens": 0, "input_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }))
    for row in get_index().message_usage(date_from, date_to):
        if not is_claude_model(row["model"]):
            continue
        entry = result[row["date"]][row["model"]]
        entry["output_tokens"] += row["output"]
        entry["input_tokens"] += row["input"]
        entry["cache_read_tokens"] += row["cache_read"]
        entry["cache_creation_tokens"] += row["cache_create"]
        entry["cost"] += usage_cost(row)
    return {d: dict(models) for d, models in result.items()}


def day_cost(date: str) -> float:
    return sum(m["cost"] for m in daily_costs(date, date).get(date, {}).values())


def get_costs() -> Optional[tuple]:
    """(daily_costs, total_cost, today_cost), or None when no pricing data is available."""
    if get_pricing() is None:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    all_daily = daily_costs()
    total = sum(m["cost"] for models in all_daily.values() for m in models.values())
    today_cost = sum(m["cost"] for m in all_daily.get(today, {}).values())
    return all_daily, total, today_cost
