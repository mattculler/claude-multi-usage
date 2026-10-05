import pytest

from claude_multi_usage import pricing
from claude_multi_usage.pricing import _match_model, _tiered_cost, calculate_model_cost, format_cost


def test_calculate_model_cost(fake_home):
    assert calculate_model_cost(fake_home.model, 100, 1000, 5000, 2000) == pytest.approx(0.0243)


def test_unknown_model_falls_back_to_sonnet_pricing(fake_home):
    assert calculate_model_cost("claude-unknown-9", 1_000_000, 0, 0, 0) == pytest.approx(3.0)


def test_no_pricing_data_does_not_raise(monkeypatch):
    monkeypatch.setattr(pricing, "get_pricing", lambda: None)
    assert _match_model("claude-sonnet-4-5") is None
    assert calculate_model_cost("claude-sonnet-4-5", 10, 10, 0, 0) == 0.0


def test_tiered_cost():
    assert _tiered_cost(100, 2.0) == 200.0
    assert _tiered_cost(300_000, 1.0, 2.0) == 200_000 * 1.0 + 100_000 * 2.0
    assert _tiered_cost(300_000, 1.0, None) == 300_000.0
    assert _tiered_cost(0, 1.0) == 0.0


@pytest.mark.parametrize("usd,expected", [
    (1234.5, "$1,234.50"),
    (0.5, "$0.50"),
    (0.001234, "$0.0012"),
])
def test_format_cost(usd, expected):
    assert format_cost(usd) == expected
