import builtins
import json
import os
import sqlite3
import threading
import time

import pytest

from claude_multi_usage import cost_cache, index, parser, pricing


def _refresh():
    idx = index.get_index()
    return idx, idx.refresh()


def _local_date(ts: str) -> str:
    return parser._utc_to_local(ts).strftime("%Y-%m-%d")


def _entry(mid, out, ts, cwd=None, model=None, **usage):
    e = {"type": "assistant", "timestamp": ts,
         "message": {"id": mid, "model": model or "claude-sonnet-4-5-20250929", "content": [],
                     "usage": {"input_tokens": usage.get("inp", 0), "output_tokens": out,
                               "cache_read_input_tokens": usage.get("cr", 0),
                               "cache_creation_input_tokens": usage.get("cc", 0)}}}
    if cwd:
        e["cwd"] = cwd
    return e


def _write(path, entries, newline=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(e) for e in entries)
    path.write_text(text + ("\n" if newline else ""))


def _fixture_entries(fake_home):
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    return [json.loads(line) for line in src.read_text().splitlines()]


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


def _myproj_tokens(idx):
    return {p.name: p.output_tokens for p in idx.projects()}["myproj"]


# -- incremental refresh ------------------------------------------------------

def test_first_refresh_indexes_everything_then_nothing(fake_home, monkeypatch):
    opened = _count_jsonl_opens(monkeypatch, lambda: index.get_index())
    assert len(opened) == 2  # both session files read once on first use

    idx = index.get_index()
    stats = idx.refresh()
    assert (stats.scanned, stats.indexed, stats.appended, stats.retired) == (2, 0, 0, 0)
    assert _count_jsonl_opens(monkeypatch, idx.refresh) == []  # unchanged files are never re-read


def test_appended_lines_are_read_from_the_old_offset(fake_home, monkeypatch):
    idx, _ = _refresh()
    before = _myproj_tokens(idx)
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    size_before = path.stat().st_size

    with open(path, "a") as f:
        f.write(json.dumps(_entry("m-late", 250, "2026-01-01T00:00:00Z", cwd="/home/alice/workspace/myproj")) + "\n")
    os.utime(path, None)

    seeks = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if str(file) == str(path):
            orig_seek = fh.seek

            def seek(pos, *a):
                seeks.append(pos)
                return orig_seek(pos, *a)
            fh.seek = seek
        return fh

    monkeypatch.setattr(builtins, "open", spy_open)
    stats = idx.refresh()
    monkeypatch.setattr(builtins, "open", real_open)

    assert (stats.indexed, stats.appended) == (0, 1)
    assert size_before in seeks                      # resumed where it left off
    assert min(seeks) >= size_before - index._TAIL_BYTES  # never re-read the body
    assert _myproj_tokens(idx) == before + 250


def test_appended_lines_in_an_existing_hour_accumulate(fake_home):
    idx, _ = _refresh()
    entries = _fixture_entries(fake_home)
    t = entries[1]["timestamp"]
    hour = parser._utc_to_local(t).hour
    before_hourly = idx.hourly(fake_home.today)[hour].message_count
    before_day = idx.day_usage(fake_home.today)[0].message_count
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(_entry("m-more", 10, t, cwd="/home/alice/workspace/myproj")) + "\n")
    os.utime(path, None)
    idx.refresh()
    assert idx.hourly(fake_home.today)[hour].message_count == before_hourly + 1
    assert idx.day_usage(fake_home.today)[0].message_count == before_day + 1


def test_append_keeps_cwd_and_first_seen(fake_home):
    d = fake_home.projects / "-home-me-foo-bar"  # directory guess would be "bar"
    path = d / "s.jsonl"
    _write(path, [_entry("a1", 10, "2026-01-01T10:00:00Z", cwd="/home/me/foo.bar")])
    idx, _ = _refresh()
    first = {p.name: p.first_seen for p in idx.projects()}["foo.bar"]
    with open(path, "a") as f:
        f.write(json.dumps(_entry("a2", 10, "2026-01-02T10:00:00Z")) + "\n")  # no cwd
    os.utime(path, None)
    stats = idx.refresh()
    assert stats.appended == 1
    projects = {p.name: p for p in idx.projects()}
    assert "bar" not in projects
    assert projects["foo.bar"].first_seen == first
    assert projects["foo.bar"].last_seen.strftime("%Y-%m-%d") == "2026-01-02"


def test_partial_trailing_line_is_deferred_until_complete(fake_home):
    idx, _ = _refresh()
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    line = json.dumps(_entry("m-partial", 999, "2026-01-01T00:00:00Z"))
    with open(path, "a") as f:
        f.write(line[:40])  # mid-write: not valid JSON yet
    idx.refresh()
    assert _myproj_tokens(idx) == fake_home.today_output_tokens
    with open(path, "a") as f:
        f.write(line[40:] + "\n")
    idx.refresh()
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 999


def test_complete_final_line_without_newline_is_counted(fake_home):
    path = fake_home.projects / "-home-alice-workspace-myproj" / "nonl.jsonl"
    _write(path, [_entry("n1", 100, "2026-01-01T00:00:00Z", cwd="/home/alice/workspace/myproj"),
                  _entry("n2", 200, "2026-01-01T00:00:01Z")], newline=False)
    idx, _ = _refresh()
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 300
    # the writer later adds the newline and another line: still exact
    with open(path, "a") as f:
        f.write("\n" + json.dumps(_entry("n3", 400, "2026-01-01T00:00:02Z")) + "\n")
    os.utime(path, None)
    stats = idx.refresh()
    assert stats.appended == 1
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 700


def test_shrunken_file_is_reindexed_in_full(fake_home):
    idx, _ = _refresh()
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:2]))  # user line + first block of m1 only
    stats = idx.refresh()
    assert (stats.indexed, stats.appended) == (1, 0)
    assert _myproj_tokens(idx) == 1000


def test_rewritten_file_of_same_size_is_reindexed_in_full(fake_home):
    path = fake_home.projects / "-home-alice-workspace-myproj" / "rw.jsonl"
    _write(path, [_entry("r1", 100, "2026-01-01T00:00:00Z", cwd="/home/alice/workspace/myproj")])
    idx, _ = _refresh()
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 100
    _write(path, [_entry("r9", 900, "2026-01-01T00:00:00Z", cwd="/home/alice/workspace/myproj")])
    assert path.stat().st_size == len(json.dumps(_entry("r1", 100, "2026-01-01T00:00:00Z", cwd="/home/alice/workspace/myproj"))) + 1
    os.utime(path, (time.time() + 5, time.time() + 5))
    stats = idx.refresh()
    assert (stats.indexed, stats.appended) == (1, 0)
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 900


def test_replaced_by_a_longer_different_file_is_reindexed_in_full(fake_home):
    path = fake_home.projects / "-home-alice-workspace-myproj" / "rp.jsonl"
    _write(path, [_entry("p1", 100, "2026-01-01T00:00:00Z", cwd="/home/alice/workspace/myproj")])
    idx, _ = _refresh()
    _write(path, [_entry("p5", 500, "2026-04-30T10:00:00Z", cwd="/home/alice/workspace/myproj"),
                  _entry("p6", 600, "2026-05-01T10:00:00Z")])
    os.utime(path, (time.time() + 5, time.time() + 5))
    stats = idx.refresh()
    assert (stats.indexed, stats.appended) == (1, 0)
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 1100
    assert cost_cache.daily_costs().get(_local_date("2026-04-30T10:00:00Z"), {}) != {}


def test_deleted_file_is_kept_as_history_and_reread_if_it_returns(fake_home):
    idx, _ = _refresh()
    path = fake_home.projects / "-home-alice-src-secret-client-acme" / "bbbb.jsonl"
    content = path.read_text()
    path.unlink()
    stats = idx.refresh()
    assert stats.retired == 1
    assert idx.file_count() == 2 and idx.file_count(include_gone=False) == 1
    # history survives Claude Code's transcript cleanup
    projects = {p.name: p for p in idx.projects()}
    assert projects["secret-client-acme"].session_count == 1
    assert projects["secret-client-acme"].output_tokens == fake_home.yday_output_tokens
    assert fake_home.yday in cost_cache.daily_costs()
    assert cost_cache.get_costs()[1] == pytest.approx(fake_home.today_cost + fake_home.yday_cost)
    # a second refresh does not retire it again
    assert idx.refresh().retired == 0
    # restored (e.g. from backup): re-read in full, nothing double counted
    path.write_text(content)
    stats = idx.refresh()
    assert (stats.indexed, stats.retired) == (1, 0)
    assert idx.file_count(include_gone=False) == 2
    assert {p.name: p.output_tokens for p in idx.projects()}["secret-client-acme"] == fake_home.yday_output_tokens


def test_rebuild_rereads_present_files_and_keeps_gone_history(fake_home, monkeypatch):
    idx, _ = _refresh()
    (fake_home.projects / "-home-alice-src-secret-client-acme" / "bbbb.jsonl").unlink()
    idx.refresh()
    opened = _count_jsonl_opens(monkeypatch, index.rebuild)
    assert len(opened) == 1  # only the file still on disk is read again
    projects = {p.name: p.output_tokens for p in index.get_index().projects()}
    assert projects == {"myproj": fake_home.today_output_tokens,
                        "secret-client-acme": fake_home.yday_output_tokens}


def test_concurrent_refreshes_do_not_double_count(fake_home, monkeypatch):
    """Two cmu processes refreshing at once (the README's background `cmu sync`)
    serialize on the write transaction instead of both adding the same lines."""
    first = index.SessionIndex(index.INDEX_FILE)
    first.refresh()
    first.close()
    entries = _fixture_entries(fake_home)
    t = entries[1]["timestamp"]
    path = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(_entry("m-race", 10, t)) + "\n")
    os.utime(path, None)

    entered, release = threading.Event(), threading.Event()
    orig = index.SessionIndex._index_file
    calls = []

    def slow_index_file(self, *args, **kwargs):
        calls.append(threading.current_thread().name)
        if not entered.is_set():
            entered.set()
            release.wait(timeout=10)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(index.SessionIndex, "_index_file", slow_index_file)
    done = {}

    def worker(name):
        idx = index.SessionIndex(index.INDEX_FILE)
        idx.refresh()
        done[name] = time.monotonic()
        idx.close()

    a = threading.Thread(target=worker, args=("A",), name="A")
    a.start()
    assert entered.wait(5)
    b = threading.Thread(target=worker, args=("B",), name="B")
    b.start()
    time.sleep(0.3)
    assert b.is_alive() and "B" not in done  # blocked on the write lock
    release.set()
    a.join(30)
    b.join(30)
    assert calls == ["A"]  # B saw the updated offset and had nothing to read
    assert done["B"] > done["A"]

    idx = index.get_index()
    hour = parser._utc_to_local(t).hour
    assert idx.hourly(fake_home.today)[hour].message_count == 7 + 1
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 10


# -- content handling ---------------------------------------------------------

def test_streamed_blocks_keep_the_final_usage(fake_home):
    """The first JSONL line of a message carries a placeholder output count."""
    ts = "2026-01-03T10:00:00Z"
    path = fake_home.projects / "-home-alice-workspace-myproj" / "stream.jsonl"
    blocks = [_entry("s1", 8, ts, cwd="/home/alice/workspace/myproj", inp=50, cr=1000),
              _entry("s1", 8, ts, inp=50, cr=1000),
              _entry("s1", 6488, ts, inp=50, cr=1000)]
    _write(path, blocks)
    idx, _ = _refresh()
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 6488
    day = cost_cache.daily_costs()[_local_date(ts)][fake_home.model]
    assert day["output_tokens"] == 6488 and day["input_tokens"] == 50 and day["cache_read_tokens"] == 1000
    # ...also when the later blocks arrive in a later refresh
    with open(path, "a") as f:
        f.write(json.dumps(_entry("s1", 7000, ts, inp=50, cr=1000)) + "\n")
    os.utime(path, None)
    idx.refresh()
    assert _myproj_tokens(idx) == fake_home.today_output_tokens + 7000


def test_subagent_files_count_tokens_but_not_sessions(fake_home):
    sub = fake_home.projects / "-home-alice-workspace-myproj" / "sess-1" / "subagents" / "agent-1.jsonl"
    entries = _fixture_entries(fake_home)
    for e in entries:
        if e["type"] == "assistant":
            e["message"]["id"] = "sub-" + e["message"]["id"]
    _write(sub, entries)

    idx, _ = _refresh()
    p = {p.name: p for p in idx.projects()}["myproj"]
    assert p.session_count == 1
    assert p.output_tokens == 2 * fake_home.today_output_tokens
    assert p.input_tokens == 2 * (100 + 5000 + 2000 + 50 + 6000)
    hourly = idx.hourly(fake_home.today)
    assert sum(h.tokens for h in hourly) == 2 * fake_home.today_output_tokens
    assert max(h.session_count for h in hourly) == 1  # subagent file is not a session
    activity, _ = idx.day_usage(fake_home.today)
    assert activity.session_count == 1


def test_nested_file_without_cwd_inherits_its_sessions_project(fake_home):
    """Some subagent transcripts carry no cwd; they belong to the parent session's
    project, not to a zero-session project guessed from the directory name."""
    d = fake_home.projects / "-home-me-foo-bar"  # directory guess would be "bar"
    _write(d / "top.jsonl", [_entry("t1", 100, "2026-01-05T10:00:00Z", cwd="/home/me/foo.bar")])
    _write(d / "sess-1" / "subagents" / "agent-x.jsonl", [_entry("nocwd", 40, "2026-01-05T10:00:00Z")])
    idx, _ = _refresh()
    projects = {p.name: p for p in idx.projects()}
    assert "bar" not in projects
    assert projects["foo.bar"].output_tokens == 140
    assert projects["foo.bar"].session_count == 1


def test_legacy_progress_entries_are_indexed(fake_home):
    ts = "2026-02-02T10:00:05Z"
    path = fake_home.projects / "-home-alice-workspace-myproj" / "old.jsonl"
    _write(path, [
        {"type": "user", "timestamp": "2026-02-02T10:00:00Z", "cwd": "/home/alice/workspace/myproj",
         "message": {"role": "user", "content": "hi"}},
        {"type": "progress", "timestamp": ts,
         "data": {"message": {"timestamp": ts,
                              "message": {"id": "sub-old", "model": fake_home.model,
                                          "usage": {"input_tokens": 0, "output_tokens": 300,
                                                    "cache_read_input_tokens": 0,
                                                    "cache_creation_input_tokens": 0}}}}},
    ])
    _refresh()
    assert cost_cache.daily_costs()[_local_date(ts)][fake_home.model]["output_tokens"] == 300


def test_synthetic_and_unknown_models_are_counted_but_not_priced(fake_home):
    ts = "2026-02-03T10:00:00Z"
    path = fake_home.projects / "-home-alice-workspace-myproj" / "synth.jsonl"
    _write(path, [_entry("sy", 0, ts, cwd="/home/alice/workspace/myproj", model="<synthetic>"),
                  {"type": "assistant", "timestamp": ts,
                   "message": {"id": "nomodel", "content": [],
                               "usage": {"input_tokens": 1000, "output_tokens": 1000}}}])
    idx, _ = _refresh()
    day = _local_date(ts)
    assert day not in cost_cache.daily_costs()           # nothing priced
    assert idx.day_usage(day)[1].tokens_by_model == {"<synthetic>": 0, "unknown": 1000}
    assert {p.name: p.cost for p in idx.projects()}["myproj"] == pytest.approx(fake_home.today_cost)


def test_mtime_fallback_is_replaced_once_timestamps_appear(fake_home):
    path = fake_home.projects / "-home-alice-workspace-myproj" / "summary.jsonl"
    _write(path, [{"type": "summary", "summary": "resumed", "cwd": "/home/alice/workspace/myproj"}])
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    idx, _ = _refresh()
    row = idx._conn.execute("SELECT ts_source, first_ts FROM files WHERE path = ?", (str(path),)).fetchone()
    assert row["ts_source"] == "mtime"
    with open(path, "a") as f:
        f.write(json.dumps(_entry("late", 5, "2025-05-01T10:00:00Z")) + "\n")
    os.utime(path, None)
    idx.refresh()
    row = idx._conn.execute("SELECT ts_source, first_ts, last_ts FROM files WHERE path = ?", (str(path),)).fetchone()
    assert row["ts_source"] == "log"
    assert row["first_ts"] == row["last_ts"] == "2025-05-01T10:00:00"


# -- pricing ------------------------------------------------------------------

def test_pricing_change_applies_without_rebuild(fake_home):
    _, total_before, _ = cost_cache.get_costs()
    pricing._pricing_cache[fake_home.model]["output"] *= 2
    _, total_after, _ = cost_cache.get_costs()
    # output tokens: 1500 today + 700 yesterday, at the extra 15e-6 per token
    assert total_after - total_before == pytest.approx(2200 * 15e-6)


def test_messages_above_the_tier_threshold_are_costed_individually(fake_home):
    pricing._pricing_cache[fake_home.model]["output_above_200k"] = 30e-6
    ts_big, ts_small = "2026-03-03T10:00:00Z", "2026-03-03T10:30:00Z"
    path = fake_home.projects / "-home-alice-workspace-myproj" / "big.jsonl"
    _write(path, [_entry("m-big", 250_000, ts_big, cwd="/home/alice/workspace/myproj"),
                  _entry("m-small", 100, ts_small)])
    idx, _ = _refresh()
    day = _local_date(ts_big)
    costs = cost_cache.daily_costs()[day][fake_home.model]
    expected = (pricing.calculate_model_cost(fake_home.model, 0, 250_000, 0, 0)
                + pricing.calculate_model_cost(fake_home.model, 0, 100, 0, 0))
    assert costs["cost"] == pytest.approx(expected)
    assert costs["output_tokens"] == 250_100
    # the large message belongs to its own day only
    assert sum(h.tokens for h in idx.hourly(fake_home.today)) == fake_home.today_output_tokens
    assert idx.day_usage(fake_home.today)[1].tokens_by_model == {fake_home.model: fake_home.today_output_tokens}
    hour = parser._utc_to_local(ts_big).hour
    assert idx.hourly(day)[hour].tokens == 250_100
    assert idx.hourly(day)[hour].cost == pytest.approx(expected)
    # ...and is included in the project totals
    p = {p.name: p for p in idx.projects()}["myproj"]
    assert p.output_tokens == fake_home.today_output_tokens + 250_100
    assert p.cost == pytest.approx(fake_home.today_cost + expected)


def test_tiered_prices_never_apply_to_sums_of_small_messages(fake_home):
    """Four 60K-cache-read messages total 240K, but no single request crossed 200K."""
    pricing._pricing_cache[fake_home.model]["cache_read_above_200k"] = 0.6e-6
    ts = "2026-03-04T10:00:00Z"
    path = fake_home.projects / "-home-alice-workspace-myproj" / "sum.jsonl"
    _write(path, [_entry(f"c{i}", 500, ts, cwd="/home/alice/workspace/myproj", inp=1000, cr=60_000)
                  for i in range(4)])
    idx, _ = _refresh()
    per_message = 4 * pricing.calculate_model_cost(fake_home.model, 1000, 500, 60_000, 0)
    assert per_message == pytest.approx(4 * (1000 * 3e-6 + 500 * 15e-6 + 60_000 * 0.3e-6))
    day = _local_date(ts)
    assert cost_cache.daily_costs()[day][fake_home.model]["cost"] == pytest.approx(per_message)
    assert cost_cache.day_cost(day) == pytest.approx(per_message)
    hour = parser._utc_to_local(ts).hour
    assert idx.hourly(day)[hour].cost == pytest.approx(per_message)
    assert {p.name: p.cost for p in idx.projects()}["myproj"] == pytest.approx(fake_home.today_cost + per_message)


# -- lifecycle ----------------------------------------------------------------

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


def test_unwritable_index_location_falls_back_to_memory(fake_home, monkeypatch, capsys):
    blocker = fake_home.home / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(index, "INDEX_FILE", blocker / "index.db")
    monkeypatch.setattr(index, "_instance", None)
    idx = index.get_index()
    assert idx.fallback_reason
    assert "session index unavailable" in capsys.readouterr().err
    assert {p.name for p in idx.projects()} == {"myproj", "secret-client-acme"}
    assert cost_cache.get_costs()[1] == pytest.approx(fake_home.today_cost + fake_home.yday_cost)


def test_cost_rebuild_flag(fake_home):
    from click.testing import CliRunner
    from claude_multi_usage import cli

    _refresh()
    result = CliRunner().invoke(cli.main, ["cost", "--rebuild"])
    assert result.exit_code == 0, result.output
    assert "rebuilt: 2 files" in result.output
    assert "$0.04" in result.output


def test_missing_projects_dir_is_fine(fake_home, monkeypatch):
    monkeypatch.setattr(parser, "PROJECTS_DIR", fake_home.home / "nope")
    idx, stats = _refresh()
    assert stats.scanned == 0
    assert idx.projects() == []
    assert all(h.tokens == 0 for h in idx.hourly(fake_home.today))
    assert idx.day_usage(fake_home.today) is None
