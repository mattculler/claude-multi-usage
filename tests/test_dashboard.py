import io

import pytest
from rich.console import Console

from claude_multi_usage import parser
from claude_multi_usage.dashboard import (
    format_tokens,
    make_models_table,
    make_summary_panel,
    make_today_hourly_chart,
)
from claude_multi_usage.parser import HourlyUsage, ModelUsage, UsageData


def render(renderable) -> str:
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False, color_system=None).print(renderable)
    return buf.getvalue()


def _empty_day():
    return [HourlyUsage(hour=h, message_count=0, session_count=0, tokens=0) for h in range(24)]


def test_local_summary_shows_cost_from_cache(fake_home):
    out = render(make_summary_panel(parser.load_usage_data()))
    assert "Estimated Cost" in out
    assert "$0.04" in out  # 0.0338 today + 0.0105 yesterday


def test_remote_summary_never_uses_local_cost(fake_home):
    data = UsageData(hostname="other-box", total_sessions=5, total_messages=9)
    out = render(make_summary_panel(data, local=False))
    assert "n/a (not synced)" in out
    assert "$" not in out


def test_remote_models_table_has_no_cost(fake_home):
    data = UsageData(hostname="x", model_usage=[ModelUsage("claude-opus-4-6", 1, 2, 3, 4)])
    out = render(make_models_table(data, local=False))
    assert "claude-opus-4-6" in out
    assert "$" not in out


def test_hourly_chart_labels_stale_remote_data():
    hourly = _empty_day()
    hourly[9] = HourlyUsage(hour=9, message_count=3, session_count=1, tokens=1200)
    out = render(make_today_hourly_chart(hourly, date_str="2020-01-01"))
    assert "Hourly Usage (2020-01-01, last sync)" in out
    assert "*" not in out  # no current-hour marker on a past day
    assert "1.2K" in out
    assert "23:00" in out  # every hour of a finished day is listed


def test_hourly_chart_defaults_to_today():
    out = render(make_today_hourly_chart(_empty_day()))
    assert "Today Hourly Usage" in out


@pytest.mark.parametrize("n,expected", [(999, "999"), (1500, "1.5K"), (2_500_000, "2.5M")])
def test_format_tokens(n, expected):
    assert format_tokens(n) == expected
