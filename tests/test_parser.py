import json

import pytest

from claude_multi_usage import cost_cache, parser
from claude_multi_usage.parser import _deduplicate_project_name


def test_today_usage_counts_each_message_once(fake_home):
    activity, tokens = parser.parse_today_usage()
    assert tokens.tokens_by_model == {fake_home.model: fake_home.today_output_tokens}
    assert activity.date == fake_home.today
    assert activity.session_count == 1
    # tool_use blocks live on separate lines and are all counted
    assert activity.tool_call_count == 2


def test_today_hourly_counts_each_message_once(fake_home):
    hourly = parser.parse_today_hourly()
    assert len(hourly) == 24
    assert sum(h.tokens for h in hourly) == fake_home.today_output_tokens
    assert sum(h.cost for h in hourly) == pytest.approx(fake_home.today_cost)


def test_projects_dedup_and_name_derivation(fake_home):
    projects = {p.name: p for p in parser.parse_projects()}
    assert projects["myproj"].output_tokens == fake_home.today_output_tokens
    assert projects["myproj"].cost == pytest.approx(fake_home.today_cost)
    # Named after the repository (last component of the recorded cwd), never
    # the encoded full path of the directory.
    other = projects["secret-client-acme"]
    assert other.output_tokens == fake_home.yday_output_tokens
    assert other.cost == pytest.approx(fake_home.yday_cost)
    assert set(projects) == {"myproj", "secret-client-acme"}


def _write_session(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_projects_with_same_repo_name_are_aggregated(fake_home):
    """Two checkouts of a repo in different parents show up as one project."""
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    entries = [json.loads(line) for line in src.read_text().splitlines()]
    for e in entries:
        e["cwd"] = "/home/alice/other/myproj"
        if e["type"] == "assistant":
            e["message"]["id"] = "other-" + e["message"]["id"]
    _write_session(fake_home.projects / "-home-alice-other-myproj" / "bbbb.jsonl", entries)

    projects = {p.name: p for p in parser.parse_projects()}
    assert projects["myproj"].session_count == 2
    assert projects["myproj"].output_tokens == 2 * fake_home.today_output_tokens
    assert projects["myproj"].cost == pytest.approx(2 * fake_home.today_cost)


def test_project_name_falls_back_to_directory_heuristic_without_cwd(fake_home):
    no_cwd = [{"type": "user", "timestamp": "2026-01-01T00:00:00Z",
               "message": {"role": "user", "content": "hi"}}]
    _write_session(fake_home.projects / "-Users-bob-workspace-tool-tool" / "s.jsonl", no_cwd)
    _write_session(fake_home.projects / "-home-bob-src-other" / "s.jsonl", no_cwd)
    _write_session(fake_home.projects / "C--Users-bob-src-winproj" / "s.jsonl", no_cwd)
    names = {p.name for p in parser.parse_projects()}
    assert "tool" in names          # "workspace" heuristic, duplicate segment collapsed
    assert "other" in names         # encoded absolute path: last segment only
    assert "winproj" in names       # Windows drive encoding: last segment only
    assert not any(n.startswith("-") or n.startswith("C--") for n in names)


@pytest.mark.parametrize("raw,expected", [
    ("-home-me-src-thing", "thing"),
    ("C--Users-bob-src-proj", "proj"),
    ("-Users-me-workspace-apr-backend-apr-backend", "apr-backend"),
    ("-Users-me-workspace-clients-acme-app", "clients-acme-app"),
])
def test_project_name_from_dir(raw, expected):
    assert parser._project_name_from_dir(raw) == expected


def test_empty_project_directories_are_ignored(fake_home):
    (fake_home.projects / "-home-alice-src-stale").mkdir()
    (fake_home.projects / "-home-alice-src-stale2").mkdir()
    (fake_home.projects / "-home-alice-src-stale2" / "notes.txt").write_text("x")
    names = {p.name for p in parser.parse_projects()}
    assert names == {"myproj", "secret-client-acme"}


def test_sessions_in_one_directory_are_named_by_their_own_cwd(fake_home):
    """'/home/me/foo.bar' and '/home/me/foo-bar' share the encoded directory
    '-home-me-foo-bar'; each session is attributed to its own repository."""
    d = fake_home.projects / "-home-me-foo-bar"
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    entries = [json.loads(line) for line in src.read_text().splitlines()]
    for i, cwd in enumerate(["/home/me/foo.bar", "/home/me/foo-bar"]):
        copy = [dict(e) for e in entries]
        for e in copy:
            e["cwd"] = cwd
            if e["type"] == "assistant":
                e["message"] = dict(e["message"], id=f"{i}-" + e["message"]["id"])
        _write_session(d / f"{i:04d}.jsonl", copy)
    projects = {p.name: p for p in parser.parse_projects()}
    assert projects["foo.bar"].output_tokens == fake_home.today_output_tokens
    assert projects["foo-bar"].output_tokens == fake_home.today_output_tokens
    assert projects["foo.bar"].session_count == projects["foo-bar"].session_count == 1


def test_worktree_sessions_count_towards_their_repository(fake_home):
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    entries = [json.loads(line) for line in src.read_text().splitlines()]
    for e in entries:
        e["cwd"] = "/home/alice/workspace/myproj/.claude/worktrees/fluffy-otter"
        if e["type"] == "assistant":
            e["message"] = dict(e["message"], id="wt-" + e["message"]["id"])
    _write_session(fake_home.projects / "-home-alice-workspace-myproj--claude-worktrees-fluffy-otter"
                   / "w.jsonl", entries)
    projects = {p.name: p for p in parser.parse_projects()}
    assert "fluffy-otter" not in projects
    assert projects["myproj"].session_count == 2
    assert projects["myproj"].output_tokens == 2 * fake_home.today_output_tokens


def test_first_and_last_seen_merge_across_naive_and_aware_datetimes(fake_home):
    """One checkout has timestamps (aware UTC); the other's log has none, so
    its dates come from the file mtime (naive local)."""
    import os
    from datetime import date

    a = [{"type": "user", "timestamp": "2026-01-10T12:00:00.000Z", "cwd": "/a/myrepo",
          "message": {"role": "user", "content": "hi"}}]
    b = [{"type": "user", "cwd": "/b/myrepo", "message": {"role": "user", "content": "hi"}}]
    _write_session(fake_home.projects / "-a-myrepo" / "s.jsonl", a)
    fb = fake_home.projects / "-b-myrepo" / "s.jsonl"
    _write_session(fb, b)
    old = parser.datetime(2025, 6, 1, 12, 0).timestamp()
    os.utime(fb, (old, old))

    for with_tokens in (True, False):
        p = {p.name: p for p in parser.parse_projects(with_tokens=with_tokens)}["myrepo"]
        assert p.session_count == 2
        assert p.first_seen.tzinfo is None and p.first_seen.date() == date(2025, 6, 1)
        if with_tokens:
            assert p.last_seen.tzinfo is not None and p.last_seen.date() == date(2026, 1, 10)
        else:  # no file is read, so both dates come from mtimes
            assert p.last_seen.tzinfo is None and p.last_seen >= p.first_seen


@pytest.mark.parametrize("cwd,expected", [
    ("/home/me/src/my-repo", "my-repo"),
    ("/home/me/src/my-repo/", "my-repo"),
    ("C:\\Users\\me\\proj", "proj"),
    ("/", "home"),
    ("/home/me/src/my-repo/.claude/worktrees/fluffy-otter", "my-repo"),
    ("/home/me/src/my-repo/.claude/worktrees/fluffy-otter/sub", "my-repo"),
    ("C:\\Users\\me\\proj\\.claude\\worktrees\\wt1", "proj"),
])
def test_repo_name_from_cwd(cwd, expected):
    assert parser._repo_name_from_cwd(cwd) == expected


def test_repo_name_for_home_directory():
    assert parser._repo_name_from_cwd(str(parser.Path.home())) == "home"


def test_read_session_cwd(fake_home, tmp_path):
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    assert parser._read_session_cwd(src) == "/home/alice/workspace/myproj"
    empty = tmp_path / "empty.jsonl"
    empty.write_text("not json\n{}\n")
    assert parser._read_session_cwd(empty) is None
    assert parser._read_session_cwd(tmp_path / "missing.jsonl") is None


def test_session_tokens_shared_seen_set_across_files(fake_home):
    """Resumed sessions copy history into a new file; shared ids stop double counting."""
    src = fake_home.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    copy = src.with_name("resumed.jsonl")
    copy.write_text(src.read_text())
    seen = set()
    out1, _, _, _ = parser._parse_session_tokens(src, seen_msg_ids=seen)
    out2, _, _, _ = parser._parse_session_tokens(copy, seen_msg_ids=seen)
    assert out1 == fake_home.today_output_tokens
    assert out2 == 0


def test_cost_cache_agrees_with_parser(fake_home):
    daily, total, today_cost = cost_cache.get_costs()
    assert today_cost == pytest.approx(fake_home.today_cost)
    assert total == pytest.approx(fake_home.today_cost + fake_home.yday_cost)
    assert set(daily) == {fake_home.today, fake_home.yday}
    # A second call serves past days from the on-disk cache and agrees
    assert cost_cache.get_costs()[1] == pytest.approx(total)
    assert (fake_home.home / ".claude-multi-usage" / "cost-cache.json").exists()


def test_load_usage_data(fake_home):
    data = parser.load_usage_data()
    assert data.hostname == "test-host"
    assert data.total_sessions == 1
    assert data.total_messages == 2
    assert data.hour_counts == {10: 1}
    assert data.projects[0].name == "myproj"
    assert data.today_hourly == []


def test_load_usage_data_without_stats_cache(fake_home):
    (fake_home.home / ".claude" / "stats-cache.json").unlink()
    data = parser.load_usage_data()
    assert data.hostname == "test-host"
    assert data.daily_activity == []


@pytest.mark.parametrize("raw,expected", [
    ("apr-backend-assignment-apr-backend-assignment", "apr-backend-assignment"),
    # documented behaviour; parse_projects() later collapses "/-" to "-"
    ("url-jarvis-url-jarvis-docs", "url-jarvis/-docs"),
    ("plain-name", "plain-name"),
    # known false positive of the heuristic
    ("foo-foobar", "foo/bar"),
])
def test_deduplicate_project_name(raw, expected):
    assert _deduplicate_project_name(raw) == expected


def test_utc_to_local_handles_bad_input():
    assert parser._utc_to_local("") is None
    assert parser._utc_to_local("not a date") is None
    assert parser._utc_to_local("2026-01-01T00:00:00Z") is not None
