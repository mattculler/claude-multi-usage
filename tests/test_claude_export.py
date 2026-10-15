import hashlib
import json
import urllib.request
import zipfile

import pytest
from click.testing import CliRunner

from claude_multi_usage import cli, config
from claude_multi_usage.claude_export import (
    DEFAULT_ALIAS,
    ESTIMATED_MODEL,
    EXPORT_HOSTNAME_PREFIX,
    PLACEHOLDER,
    UNGROUPED_PROJECT,
    ExportError,
    _local_dt,
    describe,
    device_hostname,
    estimate_tokens,
    load_export,
    message_tokens,
    summarize_export,
)
from claude_multi_usage import claude_export

ACCOUNT = {"uuid": "acct-1"}
ARTIFACT = {"content": "c" * 160}
# Shaped like a real export: account on each conversation, microsecond
# timestamps, attachments/files arrays, and the flattened `text` carrying a
# placeholder for non-text blocks.
CONVERSATIONS = [
    {"uuid": "c1", "name": "First", "account": ACCOUNT,
     "created_at": "2026-09-01T10:00:00.480257Z", "updated_at": "2026-09-02T11:00:05.000000Z",
     "chat_messages": [
         {"uuid": "m1", "sender": "human", "text": "x" * 40, "created_at": "2026-09-01T10:00:00.480257Z",
          "attachments": [], "files": []},
         {"uuid": "m2", "sender": "assistant", "text": "y" * 400, "created_at": "2026-09-01T10:00:05.1Z",
          "content": [{"type": "text", "text": "y" * 400}], "attachments": [], "files": []},
         {"uuid": "m3", "sender": "human", "text": "", "created_at": "2026-09-02T11:00:00Z",
          "content": [{"type": "text", "text": "z" * 80}],
          "attachments": [{"file_name": "notes.txt", "extracted_content": "a" * 400}], "files": []},
         {"uuid": "m4", "sender": "assistant", "text": PLACEHOLDER + PLACEHOLDER + "w" * 200,
          "created_at": "2026-09-02T11:00:05.000000+00:00",
          "content": [{"type": "thinking", "thinking": "t" * 40},
                      {"type": "tool_use", "name": "artifacts", "input": ARTIFACT},
                      {"type": "text", "text": "w" * 200}], "attachments": [], "files": []},
         {"uuid": "m7", "sender": "assistant", "text": PLACEHOLDER, "created_at": "2026-09-02T11:00:09Z",
          "attachments": [], "files": []},
     ]},
    {"uuid": "c2", "name": "Second", "account": ACCOUNT, "project": {"uuid": "p1"},
     "created_at": "2026-09-02T23:30:00Z", "chat_messages": [
         {"uuid": "m5", "sender": "human", "text": "q" * 20, "created_at": "2026-09-02T23:30:00Z"},
         {"uuid": "m6", "sender": "assistant", "text": "r" * 100, "created_at": "2026-09-02T23:30:09Z"},
     ]},
    {"uuid": "c3", "name": "Empty", "account": ACCOUNT, "created_at": "2026-09-03T00:00:00Z",
     "chat_messages": []},
]
PROJECTS = [{"uuid": "p1", "name": "Work", "created_at": "2026-08-01T00:00:00Z"}]
USERS = [{"uuid": "user-1", "full_name": "Alice", "email_address": "alice@example.com"}]
M4_OUTPUT = 10 + round(len(json.dumps(ARTIFACT, ensure_ascii=False)) / 4) + 50


def _day(ts):
    return _local_dt(ts).strftime("%Y-%m-%d")


def _write_zip(path, conversations=CONVERSATIONS, projects=PROJECTS, users=USERS, folder=""):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(folder + "conversations.json", json.dumps(conversations))
        if projects is not None:
            zf.writestr(folder + "projects.json", json.dumps(projects))
        if users is not None:
            zf.writestr(folder + "users.json", json.dumps(users))
        zf.writestr(folder + "memories.json", "[]")
    return path


@pytest.fixture
def export_zip(tmp_path):
    return _write_zip(tmp_path / "data-2026-10-06.zip")


# -- estimation -----------------------------------------------------------------

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("ab") == 1
    assert estimate_tokens("x" * 400) == 100


def test_message_tokens_reads_blocks_attachments_and_placeholders():
    msgs = {m["uuid"]: m for c in CONVERSATIONS for m in c["chat_messages"]}
    assert message_tokens(msgs["m1"]) == (10, 0)
    assert message_tokens(msgs["m2"]) == (0, 100)
    assert message_tokens(msgs["m3"]) == (20 + 100, 0)        # text block + attachment
    assert message_tokens(msgs["m4"]) == (0, M4_OUTPUT)        # thinking + artifact + text, not the placeholder
    assert message_tokens(msgs["m7"]) == (0, 0)                # placeholder only
    tool_result = {"sender": "assistant", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "o" * 80}]},
        {"type": "text", "text": "k" * 8}]}
    assert message_tokens(tool_result) == (20, 2)


@pytest.mark.parametrize("ts", ["2026-09-01T10:00:00.480257Z", "2026-09-01T10:00:05.1Z",
                                "2026-09-01T10:00:05.15303Z", "2026-09-01T10:00:00Z",
                                "2026-09-01T10:00:00.000000+00:00"])
def test_timestamps_of_any_fraction_length_parse(ts):
    assert _local_dt(ts) is not None and _local_dt(ts).tzinfo is not None


# -- loading --------------------------------------------------------------------

def test_load_export_from_zip_and_directory(export_zip, tmp_path):
    data = load_export(export_zip)
    assert [c["uuid"] for c in data["conversations"]] == ["c1", "c2", "c3"]
    assert data["projects"][0]["name"] == "Work"
    assert data["users"][0]["uuid"] == "user-1"

    extracted = tmp_path / "extracted" / "nested"
    extracted.mkdir(parents=True)
    with zipfile.ZipFile(export_zip) as zf:
        zf.extractall(extracted)
    assert load_export(tmp_path / "extracted")["conversations"] == data["conversations"]


def test_load_export_nested_folder_and_missing_siblings(tmp_path):
    path = _write_zip(tmp_path / "x.zip", projects=None, users=None, folder="data-2026-10-06/")
    data = load_export(path)
    assert len(data["conversations"]) == 3 and data["projects"] == [] and data["users"] == []


def test_load_export_rejects_bad_inputs(tmp_path, monkeypatch):
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

    # corrupted archive
    corrupt = tmp_path / "corrupt.zip"
    raw = bytearray(_write_zip(tmp_path / "ok.zip").read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    raw[len(raw) // 2 + 1] ^= 0xFF
    corrupt.write_bytes(bytes(raw))
    with pytest.raises(ExportError, match="cannot read|not valid JSON"):
        load_export(corrupt)

    # several exports in one directory
    both = tmp_path / "both"
    for name in ("a", "b"):
        (both / name).mkdir(parents=True)
        (both / name / "conversations.json").write_text("[]")
    with pytest.raises(ExportError, match="several exports"):
        load_export(both)

    # oversized
    monkeypatch.setattr(claude_export, "MAX_EXPORT_BYTES", 10)
    with pytest.raises(ExportError, match="refuses files"):
        load_export(tmp_path / "ok.zip")


# -- summary --------------------------------------------------------------------

def test_device_hostname_is_per_account(export_zip):
    data = load_export(export_zip)
    expected = f"{EXPORT_HOSTNAME_PREFIX}-{hashlib.sha256(b'acct-1').hexdigest()[:8]}"
    assert device_hostname(data) == expected
    other = {"conversations": [{"chat_messages": []}], "users": [{"uuid": "user-9"}], "projects": []}
    assert device_hostname(other) != expected and device_hostname(other).startswith("claude.ai-")
    assert device_hostname({"conversations": [], "users": [], "projects": []}) == EXPORT_HOSTNAME_PREFIX


def test_summarize_export_counts_and_estimates(export_zip):
    payload = summarize_export(load_export(export_zip), ["k1"])
    assert payload["hostname"] == device_hostname(load_export(export_zip))
    assert payload["alias"] == DEFAULT_ALIAS
    assert payload["snapshot"] is True
    assert payload["keys"] == ["k1"]
    assert payload["total_sessions"] == 2          # the empty conversation is ignored
    assert payload["total_messages"] == 7
    assert payload["today_hourly"] == [] and payload["today_hourly_date"] is None

    usage = payload["model_usage"][0]
    assert usage["model"] == ESTIMATED_MODEL
    assert usage["output_tokens"] == 100 + M4_OUTPUT + 25
    assert usage["input_tokens"] == 10 + 120 + 5

    by_date = {d["date"]: d for d in payload["daily_activity"]}
    d1, d2, d3 = _day("2026-09-01T10:00:00Z"), _day("2026-09-02T11:00:00Z"), _day("2026-09-02T23:30:00Z")
    assert by_date[d1]["message_count"] == 2 and by_date[d1]["session_count"] == 1
    if d2 == d3:
        assert by_date[d2]["message_count"] == 5 and by_date[d2]["session_count"] == 2
    else:
        assert by_date[d2]["message_count"] == 3 and by_date[d3]["message_count"] == 2
    assert all(d["tool_call_count"] == 0 for d in payload["daily_activity"])

    tokens = {t["date"]: t["tokens_by_model"][ESTIMATED_MODEL] for t in payload["daily_model_tokens"]}
    assert tokens[d1] == 100
    assert sum(tokens.values()) == usage["output_tokens"]

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
    assert "alice@example.com" not in dumped and "yyyy" not in dumped and "acct-1" not in dumped


def test_conversations_without_project_links_all_land_in_one_project(export_zip):
    data = load_export(export_zip)
    for c in data["conversations"]:
        c.pop("project", None)
    projects = {p["name"] for p in summarize_export(data, [])["projects"]}
    assert projects == {UNGROUPED_PROJECT}


def test_summarize_export_is_deterministic(export_zip):
    data = load_export(export_zip)
    a = summarize_export(data, ["k1"])
    b = summarize_export(data, ["k1"])
    a.pop("synced_at"), b.pop("synced_at")
    assert a == b


def test_describe_rows(export_zip):
    rows = dict(describe(summarize_export(load_export(export_zip), [])))
    assert rows["Conversations"] == "2" and rows["Messages"] == "7"
    assert rows["Est. output tokens"] == f"{100 + M4_OUTPUT + 25:,}" and rows["Projects"] == "2"
    assert rows["Device"].startswith("claude.ai (claude.ai-")


# -- CLI ------------------------------------------------------------------------

def test_cli_dry_run_sends_nothing(fake_home, export_zip, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call"))
    result = CliRunner().invoke(cli.main, ["import-claude-export", str(export_zip), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Conversations" in result.output and "Dry run" in result.output


def test_cli_requires_server_config(fake_home, export_zip):
    result = CliRunner().invoke(cli.main, ["import-claude-export", str(export_zip)])
    assert result.exit_code == 1
    assert "Server URL not configured" in result.output


def test_cli_reports_bad_export(fake_home, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_text("nope")
    result = CliRunner().invoke(cli.main, ["import-claude-export", str(bad), "--dry-run"])
    assert result.exit_code == 1 and "neither a ZIP" in result.output
    assert CliRunner().invoke(cli.main, ["import-claude-export", str(tmp_path / "missing.zip")]).exit_code == 2


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
    assert captured["body"]["hostname"].startswith("claude.ai-")
    assert captured["body"]["keys"] == ["k1"]
    assert captured["body"]["alias"] == "me"
    assert captured["body"]["snapshot"] is True
    assert "Imported to http://sync.invalid" in result.output


def test_reimport_replaces_the_device_on_the_server(export_zip, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from claude_multi_usage.server import app as app_module

    monkeypatch.setenv("CMU_DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.delenv("CMU_MAX_BODY_BYTES", raising=False)
    monkeypatch.setattr(app_module, "_store", None)
    data = load_export(export_zip)
    payload = summarize_export(data, ["k1"], alias="personal")
    host = payload["hostname"]
    with TestClient(app_module.app) as client:
        assert client.post("/api/sync", json=payload).status_code == 200
        assert client.post("/api/sync", json=payload).status_code == 200
        stored = client.get("/api/usage", params={"hostname": host}).json()[0]
        assert stored["daily_activity"] == payload["daily_activity"]
        assert stored["total_messages"] == payload["total_messages"]

        # a later export without the first conversation: its days disappear
        smaller = summarize_export({**data, "conversations": data["conversations"][1:]}, ["k1"])
        assert client.post("/api/sync", json=smaller).status_code == 200
        stored = client.get("/api/usage", params={"hostname": host}).json()[0]
        assert stored["daily_activity"] == smaller["daily_activity"]
        assert stored["total_messages"] == 2
        assert stored["alias"] == "claude.ai"  # the snapshot's own alias replaces the old one

        # re-importing in a different time zone cannot double count
        assert client.post("/api/sync", json=smaller).status_code == 200
        stored = client.get("/api/usage", params={"hostname": host}).json()[0]
        assert sum(d["message_count"] for d in stored["daily_activity"]) == 2


def test_estimated_usage_is_marked_in_dashboards(fake_home):
    import io
    from rich.console import Console
    from claude_multi_usage.dashboard import make_summary_panel
    from claude_multi_usage.parser import ModelUsage, UsageData

    data = UsageData(hostname="merged", model_usage=[
        ModelUsage("claude-opus-4-6", 0, 20_000, 0, 0),
        ModelUsage(ESTIMATED_MODEL, 0, 20_000, 0, 0),
    ])
    buf = io.StringIO()
    Console(file=buf, width=120, color_system=None).print(make_summary_panel(data, local=False))
    assert "40.0K (incl. ~20.0K est.)" in buf.getvalue()
