import builtins
import json
import os
import sqlite3

import pytest

from claude_multi_usage import cost_cache, index, parser, pricing


def _refresh():
    idx = index.get_index()
    return idx, idx.refresh()


def _count_jsonl_opens(monkeypatch, fn):
    opened = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if str(file).endswith(".jsonl"):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    try:
        fn()
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    return opened


def test_first_refresh_indexes_everything_then_nothing(fake_home, monkeypatch):
    opened = _count_jsonl_opens(monkeypatch, lambda: index.get_index())
    assert len(opened) == 2  # both session files read once on first use

    idx = index.get_index()
    stats = idx.refresh()
    assert (stats.scanned, stats.indexed, stats.appended, stats.removed) == (2, 0, 0, 0)
    opened = _count_jsonl_opens(monkeypatch, idx.refresh)
    assert opened == []  # unchanged files are never re-read


def test_appended_lines_are_read_from_the_old_offset(fake_home, monkeypatch):
    idx, _ = _refresh()
    before = {p.name: p.output_tokens for p in idx.projects()}
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    size_before = path.stat().st_size

    extra = {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/home/alice/workspace/myproj",
             "message": {"id": "m-late", "model": fake_home.model, "content": [],
                         "usage": {"input_tokens": 1, "output_tokens": 250,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
    with open(path, "a") as f:
        f.write(json.dumps(extra) + "\n")
    os.utime(path, None)

    reads = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if str(file) == str(path):
            orig_seek = fh.seek

            def seek(pos, *a):
                reads.append(pos)
                return orig_seek(pos, *a)
            fh.seek = seek
        return fh

    monkeypatch.setattr(builtins, "open", spy_open)
    stats = idx.refresh()
    monkeypatch.setattr(builtins, "open", real_open)

    assert (stats.indexed, stats.appended) == (0, 1)
    assert reads == [size_before]  # resumed exactly where it left off
    after = {p.name: p.output_tokens for p in idx.projects()}
    assert after["myproj"] == before["myproj"] + 250


def test_partial_trailing_line_is_deferred_until_complete(fake_home):
    idx, _ = _refresh()
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    line = json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                       "message": {"id": "m-partial", "model": fake_home.model, "content": [],
                                   "usage": {"input_tokens": 0, "output_tokens": 999,
                                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
    with open(path, "a") as f:
        f.write(line[:40])  # mid-write
    idx.refresh()
    assert {p.name: p.output_tokens for p in idx.projects()}["myproj"] == fake_home.today_output_tokens
    with open(path, "a") as f:
        f.write(line[40:] + "\n")
    idx.refresh()
    assert {p.name: p.output_tokens for p in idx.projects()}["myproj"] == fake_home.today_output_tokens + 999


def test_shrunken_file_is_reindexed_in_full(fake_home):
    idx, _ = _refresh()
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:2]))  # user line + first block of m1 only
    stats = idx.refresh()
    assert (stats.indexed, stats.appended) == (1, 0)
    assert {p.name: p.output_tokens for p in idx.projects()}["myproj"] == 1000


def test_deleted_file_is_forgotten(fake_home):
    idx, _ = _refresh()
    (fake_home.projects / "-home-alice-src-secret-client-acme" / "bbbb.jsonl").unlink()
    stats = idx.refresh()
    assert stats.removed == 1
    assert {p.name for p in idx.projects()} == {"myproj"}
    assert fake_home.yday not in cost_cache.daily_costs()


def test_subagent_files_count_tokens_but_not_sessions(fake_home):
    sub = fake_home.projects / "-home-alice-workspace-myproj" / "sess-1" / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    entries = [json.loads(line) for line in src.read_text().splitlines()]
    for e in entries:
        if e["type"] == "assistant":
            e["message"]["id"] = "sub-" + e["message"]["id"]
    sub.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    idx, _ = _refresh()
    p = {p.name: p for p in idx.projects()}["myproj"]
    assert p.session_count == 1
    assert p.output_tokens == 2 * fake_home.today_output_tokens
    hourly = idx.hourly(fake_home.today)
    assert sum(h.tokens for h in hourly) == 2 * fake_home.today_output_tokens
    assert max(h.session_count for h in hourly) == 1  # subagent file is not a session
    activity, _ = idx.day_usage(fake_home.today)
    assert activity.session_count == 1


def test_nested_file_without_cwd_inherits_its_sessions_project(fake_home):
    """Some subagent transcripts carry no cwd; they belong to the parent session's project,
    not to a zero-session project guessed from the directory name."""
    sub = fake_home.projects / "-home-alice-workspace-myproj" / "sess-1" / "subagents" / "agent-x.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps({"type": "assistant", "timestamp": "2026-01-05T10:00:00Z",
                               "message": {"id": "nocwd", "model": fake_home.model, "content": [],
                                           "usage": {"input_tokens": 0, "output_tokens": 40,
                                                     "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0}}}) + "\n")
    idx, _ = _refresh()
    projects = {p.name: p for p in idx.projects()}
    assert set(projects) == {"myproj", "secret-client-acme"}
    assert projects["myproj"].output_tokens == fake_home.today_output_tokens + 40
    assert projects["myproj"].session_count == 1


def test_legacy_progress_entries_are_indexed(fake_home):
    path = fake_home.projects / "-home-alice-workspace-myproj" / "old.jsonl"
    entries = [
        {"type": "user", "timestamp": "2026-02-02T10:00:00Z", "cwd": "/home/alice/workspace/myproj",
         "message": {"role": "user", "content": "hi"}},
        {"type": "progress", "timestamp": "2026-02-02T10:00:05Z",
         "data": {"message": {"timestamp": "2026-02-02T10:00:05Z",
                              "message": {"id": "sub-old", "model": fake_home.model,
                                          "usage": {"input_tokens": 0, "output_tokens": 300,
                                                    "cache_read_input_tokens": 0,
                                                    "cache_creation_input_tokens": 0}}}}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    idx, _ = _refresh()
    assert cost_cache.daily_costs("2026-02-02", "2026-02-02")["2026-02-02"][fake_home.model]["output_tokens"] == 300


def test_pricing_change_applies_without_rebuild(fake_home):
    _, total_before, _ = cost_cache.get_costs()
    pricing._pricing_cache[fake_home.model]["output"] *= 2
    _, total_after, _ = cost_cache.get_costs()
    assert total_after > total_before
    # output tokens: 1500 today + 700 yesterday, at the extra 15e-6 per token
    assert total_after - total_before == pytest.approx(2200 * 15e-6)


def test_messages_above_the_tier_threshold_are_costed_individually(fake_home):
    pricing._pricing_cache[fake_home.model]["output_above_200k"] = 30e-6
    big = {"type": "assistant", "timestamp": "2026-03-03T10:00:00Z", "cwd": "/home/alice/workspace/myproj",
           "message": {"id": "m-big", "model": fake_home.model, "content": [],
                       "usage": {"input_tokens": 0, "output_tokens": 250_000,
                                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
    small = dict(big, timestamp="2026-03-03T11:00:00Z",
                 message=dict(big["message"], id="m-small",
                              usage=dict(big["message"]["usage"], output_tokens=100)))
    path = fake_home.projects / "-home-alice-workspace-myproj" / "big.jsonl"
    path.write_text(json.dumps(big) + "\n" + json.dumps(small) + "\n")
    _refresh()
    day = cost_cache.daily_costs("2026-03-03", "2026-03-03")["2026-03-03"][fake_home.model]
    expected = (pricing.calculate_model_cost(fake_home.model, 0, 250_000, 0, 0)
                + pricing.calculate_model_cost(fake_home.model, 0, 100, 0, 0))
    assert day["cost"] == pytest.approx(expected)
    assert day["output_tokens"] == 250_100


def test_schema_version_mismatch_starts_over(fake_home):
    idx, _ = _refresh()
    assert idx.file_count() == 2
    idx.close()
    conn = sqlite3.connect(index.INDEX_FILE)
    conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    reopened = index.SessionIndex(index.INDEX_FILE)
    assert reopened.file_count() == 0
    reopened.refresh()
    assert reopened.file_count() == 2
    reopened.close()
    index._instance = None


def test_rebuild_deletes_the_index_file(fake_home):
    _refresh()
    assert index.INDEX_FILE.exists()
    index.rebuild()
    assert not index.INDEX_FILE.exists()
    assert index._instance is None
    assert {p.name for p in parser.parse_projects()} == {"myproj", "secret-client-acme"}


def test_cost_rebuild_flag(fake_home):
    from click.testing import CliRunner
    from claude_multi_usage import cli

    _refresh()
    result = CliRunner().invoke(cli.main, ["cost", "--rebuild"])
    assert result.exit_code == 0, result.output
    assert "re-reading" in result.output
    assert "$0.04" in result.output


def test_missing_projects_dir_is_fine(fake_home, monkeypatch):
    monkeypatch.setattr(parser, "PROJECTS_DIR", fake_home.home / "nope")
    idx, stats = _refresh()
    assert stats.scanned == 0
    assert idx.projects() == []
    assert idx.hourly(fake_home.today) == [] or all(h.tokens == 0 for h in idx.hourly(fake_home.today))
    assert idx.day_usage(fake_home.today) is None
