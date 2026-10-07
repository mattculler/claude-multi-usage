import json
import urllib.request
import zipfile

import pytest
from click.testing import CliRunner

from claude_multi_usage import cli, config
from claude_multi_usage.claude_export import (
    ESTIMATED_MODEL,
    EXPORT_HOSTNAME,
    UNGROUPED_PROJECT,
    ExportError,
    _local_dt,
    describe,
    estimate_tokens,
    load_export,
    summarize_export,
)

CONVERSATIONS = [
    {"uuid": "c1", "name": "First", "created_at": "2026-09-01T10:00:00Z", "chat_messages": [
        {"uuid": "m1", "sender": "human", "text": "x" * 40, "created_at": "2026-09-01T10:00:00Z"},
        {"uuid": "m2", "sender": "assistant", "text": "y" * 400, "created_at": "2026-09-01T10:00:05Z"},
        # text empty, content blocks carry the words (newer export shape)
        {"uuid": "m3", "sender": "human", "text": "", "created_at": "2026-09-02T11:00:00Z",
         "content": [{"type": "text", "text": "z" * 80}]},
        {"uuid": "m4", "sender": "assistant", "text": "w" * 200, "created_at": "2026-09-02T11:00:05Z"},
    ]},
    {"uuid": "c2", "name": "Second", "project_uuid": "p1", "created_at": "2026-09-02T23:30:00Z",
     "chat_messages": [
         {"uuid": "m5", "sender": "human", "text": "q" * 20, "created_at": "2026-09-02T23:30:00Z"},
         {"uuid": "m6", "sender": "assistant", "text": "r" * 100, "created_at": "2026-09-02T23:30:09Z"},
     ]},
    {"uuid": "c3", "name": "Empty", "created_at": "2026-09-03T00:00:00Z", "chat_messages": []},
]
PROJECTS = [{"uuid": "p1", "name": "Work", "created_at": "2026-08-01T00:00:00Z"}]
USERS = [{"uuid": "u1", "full_name": "Alice", "email_address": "alice@example.com"}]


def _day(ts):
    return _local_dt(ts).strftime("%Y-%m-%d")


@pytest.fixture
def export_zip(tmp_path):
    path = tmp_path / "data-2026-10-06.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps(CONVERSATIONS))
        zf.writestr("projects.json", json.dumps(PROJECTS))
        zf.writestr("users.json", json.dumps(USERS))
        zf.writestr("memories.json", "[]")
    return path


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("ab") == 1
    assert estimate_tokens("x" * 400) == 100


def test_load_export_from_zip_and_directory(export_zip, tmp_path):
    data = load_export(export_zip)
    assert [c["uuid"] for c in data["conversations"]] == ["c1", "c2", "c3"]
    assert data["projects"][0]["name"] == "Work"
    assert data["users"][0]["uuid"] == "u1"

    extracted = tmp_path / "extracted" / "nested"
    extracted.mkdir(parents=True)
    with zipfile.ZipFile(export_zip) as zf:
        zf.extractall(extracted)
    assert load_export(tmp_path / "extracted")["conversations"] == data["conversations"]


def test_load_export_rejects_other_inputs(tmp_path):
    not_zip = tmp_path / "x.zip"
    not_zip.write_text("hello")
    with pytest.raises(ExportError, match="neither a ZIP"):
        load_export(not_zip)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ExportError, match="no conversations.json"):
        load_export(empty_dir)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("conversations.json", "{not json")
    with pytest.raises(ExportError, match="not valid JSON"):
        load_export(bad)
    wrong_shape = tmp_path / "shape.zip"
    with zipfile.ZipFile(wrong_shape, "w") as zf:
        zf.writestr("conversations.json", json.dumps({"a": 1}))
    with pytest.raises(ExportError, match="list of conversations"):
        load_export(wrong_shape)


def test_summarize_export_counts_and_estimates(export_zip):
    payload = summarize_export(load_export(export_zip), ["k1"], alias="my account")
    assert payload["hostname"] == EXPORT_HOSTNAME
    assert payload["alias"] == "my account"
    assert payload["keys"] == ["k1"]
    assert payload["total_sessions"] == 2          # the empty conversation is ignored
    assert payload["total_messages"] == 6
    assert payload["today_hourly"] == [] and payload["today_hourly_date"] is None

    usage = payload["model_usage"][0]
    assert usage["model"] == ESTIMATED_MODEL
    assert usage["output_tokens"] == 100 + 50 + 25   # y*400, w*200, r*100
    assert usage["input_tokens"] == 10 + 20 + 5       # x*40, z*80 (content block), q*20

    by_date = {d["date"]: d for d in payload["daily_activity"]}
    d1, d2, d3 = _day("2026-09-01T10:00:00Z"), _day("2026-09-02T11:00:00Z"), _day("2026-09-02T23:30:00Z")
    assert by_date[d1]["message_count"] == 2 and by_date[d1]["session_count"] == 1
    if d2 == d3:
        assert by_date[d2]["message_count"] == 4 and by_date[d2]["session_count"] == 2
    else:
        assert by_date[d2]["message_count"] == 2 and by_date[d3]["message_count"] == 2
    assert all(d["tool_call_count"] == 0 for d in payload["daily_activity"])

    tokens = {t["date"]: t["tokens_by_model"][ESTIMATED_MODEL] for t in payload["daily_model_tokens"]}
    assert tokens[d1] == 100
    assert sum(tokens.values()) == 175

    hours = {_local_dt("2026-09-01T10:00:00Z").hour, _local_dt("2026-09-02T23:30:00Z").hour}
    assert sum(payload["hour_counts"].values()) == 2
    assert set(int(h) for h in payload["hour_counts"]) == hours

    projects = {p["name"]: p for p in payload["projects"]}
    assert set(projects) == {UNGROUPED_PROJECT, "Work"}
    assert projects["Work"]["session_count"] == 1 and projects["Work"]["output_tokens"] == 25
    assert projects[UNGROUPED_PROJECT]["session_count"] == 1
    assert projects[UNGROUPED_PROJECT]["first_seen"].startswith(d1)
    assert payload["first_session_date"].startswith(d1)

    # nothing sensitive: no message text, no user record
    dumped = json.dumps(payload)
    assert "alice@example.com" not in dumped and "yyyy" not in dumped


def test_summarize_export_is_deterministic(export_zip):
    data = load_export(export_zip)
    a = summarize_export(data, ["k1"])
    b = summarize_export(data, ["k1"])
    a.pop("synced_at"), b.pop("synced_at")
    assert a == b


def test_describe_rows(export_zip):
    rows = dict(describe(summarize_export(load_export(export_zip), [])))
    assert rows["Conversations"] == "2" and rows["Messages"] == "6"
    assert rows["Est. output tokens"] == "175" and rows["Projects"] == "2"


def test_cli_dry_run_sends_nothing(fake_home, export_zip, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call"))
    result = CliRunner().invoke(cli.main, ["import-claude-export", str(export_zip), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Conversations" in result.output and "Dry run" in result.output


def test_cli_requires_server_config(fake_home, export_zip):
    result = CliRunner().invoke(cli.main, ["import-claude-export", str(export_zip)])
    assert result.exit_code == 1
    assert "Server URL not configured" in result.output


def test_cli_imports_as_claude_ai_device(fake_home, export_zip, monkeypatch):
    config.set_server_url("http://sync.invalid")
    config.add_key("k1")
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"status":"ok"}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = CliRunner().invoke(cli.main, ["import-claude-export", str(export_zip), "--alias", "me"])
    assert result.exit_code == 0, result.output
    assert captured["url"] == "http://sync.invalid/api/sync"
    assert captured["body"]["hostname"] == EXPORT_HOSTNAME
    assert captured["body"]["keys"] == ["k1"]
    assert captured["body"]["alias"] == "me"
    assert "Imported to http://sync.invalid" in result.output


def test_reimport_is_idempotent_on_the_server(export_zip, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from claude_multi_usage.server import app as app_module

    monkeypatch.setenv("CMU_DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.delenv("CMU_MAX_BODY_BYTES", raising=False)
    monkeypatch.setattr(app_module, "_store", None)
    payload = summarize_export(load_export(export_zip), ["k1"])
    with TestClient(app_module.app) as client:
        assert client.post("/api/sync", json=payload).status_code == 200
        assert client.post("/api/sync", json=payload).status_code == 200
        stored = client.get("/api/usage", params={"hostname": EXPORT_HOSTNAME}).json()[0]
        assert stored["daily_activity"] == payload["daily_activity"]
        assert stored["total_messages"] == payload["total_messages"]

        # a newer export with more messages on an existing day replaces that day
        newer = json.loads(json.dumps(payload))
        newer["daily_activity"][0]["message_count"] += 5
        newer["total_messages"] += 5
        assert client.post("/api/sync", json=newer).status_code == 200
        stored = client.get("/api/usage", params={"hostname": EXPORT_HOSTNAME}).json()[0]
        assert stored["daily_activity"][0]["message_count"] == payload["daily_activity"][0]["message_count"] + 5
        assert len(stored["daily_activity"]) == len(payload["daily_activity"])
