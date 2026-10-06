import json
import urllib.request

from click.testing import CliRunner

from claude_multi_usage import cli, config


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_bad_date_is_a_usage_error(fake_home):
    result = CliRunner().invoke(cli.main, ["dashboard", "--from", "garbage"])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


def test_days_must_be_positive(fake_home):
    assert CliRunner().invoke(cli.main, ["dashboard", "-d", "0"]).exit_code == 2


def test_today_reports_deduplicated_tokens_and_cost(fake_home):
    result = CliRunner().invoke(cli.main, ["today"])
    assert result.exit_code == 0, result.output
    assert "1.5K" in result.output
    assert "$0.03" in result.output


def test_dashboard_runs(fake_home):
    result = CliRunner().invoke(cli.main, ["dashboard", "-d", "3"])
    assert result.exit_code == 0, result.output
    assert "test-host" in result.output
    assert "Top Projects" in result.output


def test_tree_runs(fake_home):
    result = CliRunner().invoke(cli.main, ["tree"])
    assert result.exit_code == 0, result.output
    assert "This Year" in result.output


def test_config_roundtrip(fake_home):
    runner = CliRunner()
    assert runner.invoke(cli.main, ["config", "server", "http://127.0.0.1:1/"]).exit_code == 0
    assert runner.invoke(cli.main, ["config", "key", "add", "k1", "desc"]).exit_code == 0
    assert runner.invoke(cli.main, ["config", "alias", "box"]).exit_code == 0
    assert config.load_config() == {
        "server_url": "http://127.0.0.1:1",
        "keys": [{"key": "k1", "description": "desc"}],
        "alias": "box",
    }
    out = runner.invoke(cli.main, ["config", "show"]).output
    assert "k1" in out and "box" in out
    assert runner.invoke(cli.main, ["config", "key", "remove", "k1"]).exit_code == 0
    assert config.get_keys() == []


def test_sync_without_config_fails_cleanly(fake_home):
    result = CliRunner().invoke(cli.main, ["sync"])
    assert result.exit_code == 1
    assert "Server URL not configured" in result.output


def test_sync_payload_contents(fake_home, monkeypatch):
    config.set_server_url("http://sync.invalid")
    config.add_key("k1")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _FakeResponse(b'{"status":"ok"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = CliRunner().invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output

    body = captured["body"]
    assert captured["url"] == "http://sync.invalid/api/sync"
    assert body["hostname"] == "test-host"
    assert body["keys"] == ["k1"]
    # cumulative figures the diff view needs
    assert body["total_sessions"] == 1 and body["total_messages"] == 2
    assert body["model_usage"][0]["model"] == fake_home.model
    assert body["hour_counts"] == {"10": 1}
    assert body["today_hourly_date"] == fake_home.today
    assert sum(h["tokens"] for h in body["today_hourly"]) == fake_home.today_output_tokens
    # repository names only, never the full local path
    assert {p["name"] for p in body["projects"]} == {"myproj", "secret-client-acme"}
    assert "/home/alice" not in json.dumps(body)
    # never any message content
    assert "content" not in json.dumps(body)


def test_diff_renders_remote_totals_without_local_cost(fake_home, monkeypatch):
    config.set_server_url("http://sync.invalid")
    config.add_key("k1")
    device = {
        "hostname": "other-box", "alias": "office", "keys": ["k1"], "synced_at": "x",
        "total_sessions": 42, "total_messages": 999, "first_session_date": "2026-01-01",
        "model_usage": [{"model": "claude-opus-4-6", "input_tokens": 1, "output_tokens": 2000,
                         "cache_read_tokens": 3, "cache_creation_tokens": 4}],
        "daily_activity": [{"date": fake_home.yday, "message_count": 5, "session_count": 1,
                            "tool_call_count": 0}],
        "daily_model_tokens": [{"date": fake_home.yday, "tokens_by_model": {"claude-opus-4-6": 2000}}],
        "projects": [{"name": "remote-proj", "session_count": 1, "output_tokens": 2000}],
        "hour_counts": {"9": 1},
        "today_hourly_date": "2020-01-01",
        "today_hourly": [{"hour": 9, "message_count": 1, "session_count": 1, "tokens": 2000}],
    }
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResponse(json.dumps([device]).encode()),
    )

    result = CliRunner().invoke(cli.main, ["diff"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "office (other-box)" in out
    assert "42" in out and "999" in out
    assert "claude-opus-4-6" in out
    assert "remote-proj" in out
    assert "n/a (not synced)" in out
    assert "Hourly Usage (2020-01-01, last sync)" in out
    # nothing from this machine's cost cache is attributed to the remote device
    assert "$" not in out

    merged = CliRunner().invoke(cli.main, ["diff", "--merged"])
    assert merged.exit_code == 0, merged.output
    assert "42" in merged.output
    assert "$" not in merged.output
