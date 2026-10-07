"""Shared fixtures: a synthetic ~/.claude tree and in-memory pricing.

Every module-level path constant is monkeypatched so the tests never touch
the real home directory, and pricing is injected so nothing hits the network.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from claude_multi_usage import cli, config, cost_cache, dashboard, index, parser, pricing, tree

MODEL = "claude-sonnet-4-5-20250929"
PRICING = {
    MODEL: {
        "input": 3e-6, "output": 15e-6,
        "cache_read": 0.3e-6, "cache_creation": 3.75e-6,
        "input_above_200k": None, "output_above_200k": None,
        "cache_read_above_200k": None, "cache_creation_above_200k": None,
    }
}

# Session A (today) holds two assistant messages.  Each is written as several
# JSONL lines (one per content block) that repeat the same id and usage,
# exactly as Claude Code does.  Deduplicated: 1000 + 500 = 1500 output tokens.
#   m1: 100 in, 1000 out, 5000 cache read, 2000 cache create -> $0.0243
#   m2:  50 in,  500 out, 6000 cache read,    0 cache create -> $0.00945
TODAY_OUTPUT_TOKENS = 1500
TODAY_COST = 0.0243 + 0.00945
# Session B (yesterday) holds one assistant message: 10 in / 700 out -> $0.01053
YDAY_OUTPUT_TOKENS = 700
YDAY_COST = 0.01053


def _asst(mid, inp, out, cache_read, cache_create, ts, block):
    return {
        "type": "assistant", "timestamp": ts,
        "message": {
            "id": mid, "model": MODEL, "role": "assistant", "content": [block],
            "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
        },
    }


def _utc_ts(local_dt: datetime) -> str:
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Build a fake ~/.claude and point every module at it."""
    claude_dir = tmp_path / ".claude"
    projects = claude_dir / "projects"
    cmu_dir = tmp_path / ".claude-multi-usage"

    monkeypatch.setattr(parser, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(parser, "STATS_FILE", claude_dir / "stats-cache.json")
    monkeypatch.setattr(parser, "PROJECTS_DIR", projects)
    monkeypatch.setattr(parser, "get_hostname", lambda: "test-host")
    monkeypatch.setattr(index, "CACHE_DIR", cmu_dir)
    monkeypatch.setattr(index, "INDEX_FILE", cmu_dir / "index.db")
    monkeypatch.setattr(index, "_instance", None)
    monkeypatch.setattr(pricing, "CACHE_DIR", cmu_dir)
    monkeypatch.setattr(pricing, "PRICING_CACHE_FILE", cmu_dir / "pricing-cache.json")
    monkeypatch.setattr(pricing, "_pricing_cache", {k: dict(v) for k, v in PRICING.items()})
    monkeypatch.setattr(pricing, "_fetch_litellm_pricing", lambda: None)
    monkeypatch.setattr(config, "CONFIG_DIR", cmu_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", cmu_dir / "config.json")
    # Rich reads COLUMNS when stdout is not a terminal; keep table cells unwrapped.
    monkeypatch.setenv("COLUMNS", "140")

    # Freeze "now" for every module that asks, so a run that straddles local
    # midnight sees the same "today" as the fixture's timestamps.
    frozen = datetime.now()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz is not None else frozen

    for mod in (parser, cost_cache, cli, dashboard, tree, index):
        monkeypatch.setattr(mod, "datetime", FrozenDatetime)

    now_local = frozen.astimezone()
    today = now_local.strftime("%Y-%m-%d")
    yday_noon = (now_local - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    yday = yday_noon.strftime("%Y-%m-%d")

    t = _utc_ts(now_local)
    session_a = [
        {"type": "user", "timestamp": t, "message": {"role": "user", "content": "hi"}},
        _asst("m1", 100, 1000, 5000, 2000, t, {"type": "text", "text": "a"}),
        _asst("m1", 100, 1000, 5000, 2000, t, {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        _asst("m1", 100, 1000, 5000, 2000, t, {"type": "text", "text": "b"}),
        {"type": "user", "timestamp": t,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
        _asst("m2", 50, 500, 6000, 0, t, {"type": "text", "text": "c"}),
        _asst("m2", 50, 500, 6000, 0, t, {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}),
    ]
    ty = _utc_ts(yday_noon)
    session_b = [
        {"type": "user", "timestamp": ty, "message": {"role": "user", "content": "hi"}},
        _asst("m3", 10, 700, 0, 0, ty, {"type": "text", "text": "d"}),
    ]

    # Claude Code stamps every entry with the session's working directory;
    # the project name is its last component.
    for entry in session_a:
        entry["cwd"] = "/home/alice/workspace/myproj"
    for entry in session_b:
        entry["cwd"] = "/home/alice/src/secret-client-acme"

    dir_a = projects / "-home-alice-workspace-myproj"
    dir_b = projects / "-home-alice-src-secret-client-acme"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "aaaa.jsonl").write_text("\n".join(json.dumps(x) for x in session_a) + "\n")
    file_b = dir_b / "bbbb.jsonl"
    file_b.write_text("\n".join(json.dumps(x) for x in session_b) + "\n")
    mtime = yday_noon.timestamp()
    os.utime(file_b, (mtime, mtime))

    (claude_dir / "stats-cache.json").write_text(json.dumps({
        "dailyActivity": [{"date": yday, "messageCount": 2, "sessionCount": 1, "toolCallCount": 0}],
        "dailyModelTokens": [{"date": yday, "tokensByModel": {MODEL: YDAY_OUTPUT_TOKENS}}],
        "modelUsage": {MODEL: {"inputTokens": 10, "outputTokens": YDAY_OUTPUT_TOKENS,
                               "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
        "hourCounts": {"10": 1},
        "totalSessions": 1,
        "totalMessages": 2,
        "firstSessionDate": yday + "T10:00:00.000Z",
    }))

    yield SimpleNamespace(
        home=tmp_path, projects=projects, today=today, yday=yday, model=MODEL,
        today_output_tokens=TODAY_OUTPUT_TOKENS, today_cost=TODAY_COST,
        yday_output_tokens=YDAY_OUTPUT_TOKENS, yday_cost=YDAY_COST,
    )
    if index._instance is not None:
        index._instance.close()
