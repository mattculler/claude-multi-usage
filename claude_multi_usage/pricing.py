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

# 200K 토큰 초과 tiered pricing threshold
TIERED_THRESHOLD = 200_000

# 캐시된 pricing 데이터 (메모리)
_pricing_cache: Optional[dict] = None


def _fetch_litellm_pricing() -> Optional[dict]:
    """LiteLLM pricing JSON을 fetch하여 Claude 모델만 추출."""
    try:
        req = urllib.request.Request(
            LITELLM_PRICING_URL,
            headers={"User-Agent": "claude-multi-usage"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            all_models = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None

    # Claude 모델만 필터링
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
    """로컬 캐시에서 pricing 데이터 로드.

    ignore_expiry=True면 만료 여부 무시하고 데이터가 있으면 반환 (fallback용).
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
    1. 메모리 캐시 → 2. 로컬 캐시 (24h 이내) → 3. LiteLLM fetch → 4. 만료된 로컬 캐시 → 5. None
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache

    # 로컬 캐시 확인 (24시간 이내)
    data = _load_pricing_cache()
    if data is not None:
        _pricing_cache = data
        return data

    # LiteLLM에서 fetch
    data = _fetch_litellm_pricing()
    if data is not None:
        _save_pricing_cache(data)
        _pricing_cache = data
        return data

    # Fallback: 만료된 캐시라도 있으면 사용
    data = _load_pricing_cache(ignore_expiry=True)
    if data is not None:
        _pricing_cache = data
        return data

    # 가격 데이터 없음
    return None


def _match_model(model_id: str) -> Optional[dict]:
    """모델 ID에서 pricing 데이터 매칭.

    정확한 키 매칭 → 부분 매칭 → fallback 순서.
    """
    pricing = get_pricing()
    if not pricing:
        return None

    # 정확 매칭
    if model_id in pricing:
        return pricing[model_id]

    # 부분 매칭 (긴 키 우선으로 정렬하여 가장 구체적인 매칭)
    for key in sorted(pricing.keys(), key=len, reverse=True):
        if key in model_id or model_id in key:
            return pricing[key]

    # fallback: sonnet 가격
    for key in pricing:
        if "sonnet" in key:
            return pricing[key]

    return None


def _tiered_cost(tokens: int, base_price: float,
                 tiered_price: Optional[float] = None) -> float:
    """200K 초과 tiered pricing 적용."""
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
