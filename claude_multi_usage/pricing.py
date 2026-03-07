"""Anthropic API pricing for cost estimation.

Prices are per 1M tokens (USD).
Source: https://www.anthropic.com/pricing
"""

from __future__ import annotations

# (input, output, cache_read, cache_creation) per 1M tokens
MODEL_PRICING = {
    "claude-opus-4-6": (15.0, 75.0, 1.50, 18.75),
    "claude-opus-4-5": (15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (0.80, 4.0, 0.08, 1.0),
}

# 모델 ID에서 가격표 키 매칭
def _match_model(model_id: str) -> str:
    for key in MODEL_PRICING:
        if key in model_id:
            return key
    # fallback: sonnet 가격 적용
    return "claude-sonnet-4-5"


def calculate_model_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    """Calculate cost in USD for a model's token usage."""
    key = _match_model(model)
    p_in, p_out, p_cache_read, p_cache_create = MODEL_PRICING[key]

    cost = (
        (input_tokens / 1_000_000) * p_in
        + (output_tokens / 1_000_000) * p_out
        + (cache_read_tokens / 1_000_000) * p_cache_read
        + (cache_creation_tokens / 1_000_000) * p_cache_create
    )
    return cost


def format_cost(usd: float) -> str:
    if usd >= 1.0:
        return f"${usd:,.2f}"
    if usd >= 0.01:
        return f"${usd:.2f}"
    return f"${usd:.4f}"
