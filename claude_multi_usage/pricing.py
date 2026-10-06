"""Anthropic API pricing for cost estimation.

Fetches model pricing from LiteLLM's pricing DB and caches locally.
Falls back to last cached data if fetch fails, or returns None if no data available.

Source: https://github.com/BerriAI/litellm
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

CACHE_DIR = Path.home() / ".claude-multi-usage"
PRICING_CACHE_FILE = CACHE_DIR / "pricing-cache.json"
PRICING_CACHE_MAX_AGE_HOURS = 24

# Tiered pricing threshold (above 200K tokens)
TIERED_THRESHOLD = 200_000

# In-memory pricing cache
_pricing_cache: Optional[dict] = None


def _fetch_litellm_pricing() -> Optional[dict]:
    """Fetch LiteLLM's pricing JSON and keep only Claude models."""
    try:
        req = urllib.request.Request(
            LITELLM_PRICING_URL,
            headers={"User-Agent": "claude-multi-usage"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            all_models = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None

    # Keep only Claude models
    claude_models = {}
    for key, info in all_models.items():
        if "claude" not in key:
            continue
        if not isinstance(info, dict):
            continue
        if "input_cost_per_token" not in info:
            continue
        claude_models[key] = {
            "input": info.get("input_cost_per_token", 0),
            "output": info.get("output_cost_per_token", 0),
            "cache_read": info.get("cache_read_input_token_cost", 0),
            "cache_creation": info.get("cache_creation_input_token_cost", 0),
            "input_above_200k": info.get("input_cost_per_token_above_200k_tokens"),
            "output_above_200k": info.get("output_cost_per_token_above_200k_tokens"),
            "cache_read_above_200k": info.get("cache_read_input_token_cost_above_200k_tokens"),
            "cache_creation_above_200k": info.get("cache_creation_input_token_cost_above_200k_tokens"),
        }

    return claude_models


def _save_pricing_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = {
        "fetched_at": datetime.now().isoformat(),
        "models": data,
    }
    with open(PRICING_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _load_pricing_cache(ignore_expiry: bool = False) -> Optional[dict]:
    """Load pricing data from the local cache.

    With ignore_expiry=True the data is returned even if stale (fallback).
    """
    if not PRICING_CACHE_FILE.exists():
        return None
    try:
        with open(PRICING_CACHE_FILE) as f:
            cache = json.load(f)
        if not ignore_expiry:
            fetched_at = datetime.fromisoformat(cache["fetched_at"])
            age_hours = (datetime.now() - fetched_at).total_seconds() / 3600
            if age_hours > PRICING_CACHE_MAX_AGE_HOURS:
                return None
        return cache.get("models")
    except (json.JSONDecodeError, KeyError, OSError, ValueError):
        return None


def get_pricing() -> Optional[dict]:
    """Get pricing data (from cache, fetch, or last cached fallback).

    Returns dict or None if no pricing data is available at all.
    1. memory cache -> 2. local cache (<24h) -> 3. LiteLLM fetch -> 4. stale local cache -> 5. None
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache

    # Local cache (within 24 hours)
    data = _load_pricing_cache()
    if data is not None:
        _pricing_cache = data
        return data

    # Fetch from LiteLLM
    data = _fetch_litellm_pricing()
    if data is not None:
        _save_pricing_cache(data)
        _pricing_cache = data
        return data

    # Fallback: use a stale cache if one exists
    data = _load_pricing_cache(ignore_expiry=True)
    if data is not None:
        _pricing_cache = data
        return data

    # No pricing data available
    return None


def _match_model(model_id: str) -> Optional[dict]:
    """Match a model ID to pricing data.

    Order: exact key match -> partial match -> fallback.
    """
    pricing = get_pricing()
    if not pricing:
        return None

    # Exact match
    if model_id in pricing:
        return pricing[model_id]

    # Partial match (longest keys first so the most specific entry wins)
    for key in sorted(pricing.keys(), key=len, reverse=True):
        if key in model_id or model_id in key:
            return pricing[key]

    # Fallback: sonnet pricing
    for key in pricing:
        if "sonnet" in key:
            return pricing[key]

    return None


def _tiered_cost(tokens: int, base_price: float,
                 tiered_price: Optional[float] = None) -> float:
    """Apply tiered pricing above the 200K threshold."""
    if tokens <= 0 or base_price is None:
        return 0.0
    if tiered_price is not None and tokens > TIERED_THRESHOLD:
        below = TIERED_THRESHOLD * base_price
        above = (tokens - TIERED_THRESHOLD) * tiered_price
        return below + above
    return tokens * base_price


def calculate_model_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    """Calculate cost in USD for a model's token usage."""
    info = _match_model(model)
    if info is None:
        return 0.0

    cost = (
        _tiered_cost(input_tokens, info.get("input", 0),
                     info.get("input_above_200k"))
        + _tiered_cost(output_tokens, info.get("output", 0),
                       info.get("output_above_200k"))
        + _tiered_cost(cache_read_tokens, info.get("cache_read", 0),
                       info.get("cache_read_above_200k"))
        + _tiered_cost(cache_creation_tokens, info.get("cache_creation", 0),
                       info.get("cache_creation_above_200k"))
    )
    return cost


def format_cost(usd: float) -> str:
    if usd >= 1.0:
        return f"${usd:,.2f}"
    if usd >= 0.01:
        return f"${usd:.2f}"
    return f"${usd:.4f}"
