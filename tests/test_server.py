import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_multi_usage.server import app as app_module  # noqa: E402
from claude_multi_usage.server.models import SyncPayload  # noqa: E402
from claude_multi_usage.server.store import Store  # noqa: E402


def _make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CMU_DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.setattr(app_module, "_store", None)
    return TestClient(app_module.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as c:
        yield c


def payload(hostname="box-a", **overrides):
    base = {
        "hostname": hostname, "synced_at": "2026-10-05T10:00:00", "keys": ["k1"],
        "daily_activity": [{"date": "2026-10-04", "message_count": 2, "session_count": 1,
                            "tool_call_count": 0}],
        "daily_model_tokens": [{"date": "2026-10-04", "tokens_by_model": {"claude-opus-4-6": 700}}],
        "projects": [{"name": "p1", "session_count": 1, "output_tokens": 700}],
        "today_hourly_date": "2026-10-05",
        "today_hourly": [{"hour": 9, "message_count": 1, "session_count": 1, "tokens": 100}],
        "total_sessions": 3, "total_messages": 10,
    }
    base.update(overrides)
    return base


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_sync_then_filter_by_key_and_hostname(client):
    assert client.post("/api/sync", json=payload()).json() == {"status": "ok", "hostname": "box-a"}
    assert client.post("/api/sync", json=payload("box-b", keys=["k2"])).status_code == 200

    k1 = client.get("/api/usage", params={"key": "k1"}).json()
    assert [d["hostname"] for d in k1] == ["box-a"]
    assert k1[0]["total_sessions"] == 3
    assert k1[0]["today_hourly_date"] == "2026-10-05"

    assert [d["hostname"] for d in client.get("/api/devices", params={"key": "k2"}).json()] == ["box-b"]

    by_host = client.get("/api/usage", params={"hostname": "box-b"}).json()
    assert len(by_host) == 1 and by_host[0]["keys"] == ["k2"]
    assert client.get("/api/usage", params={"hostname": "nope"}).json() == []


def test_resync_merges_history_and_keeps_alias(client):
    client.post("/api/sync", json=payload(alias="first"))
    second = payload(
        alias=None, synced_at="2026-10-06T10:00:00", today_hourly_date="2026-10-06",
        daily_activity=[{"date": "2026-10-06", "message_count": 1, "session_count": 1, "tool_call_count": 0}],
        daily_model_tokens=[{"date": "2026-10-06", "tokens_by_model": {"claude-opus-4-6": 50}}],
        projects=[{"name": "p2", "session_count": 1, "output_tokens": 50}],
        total_sessions=4,
    )
    client.post("/api/sync", json=second)

    d = client.get("/api/usage", params={"hostname": "box-a"}).json()[0]
    assert sorted(x["date"] for x in d["daily_model_tokens"]) == ["2026-10-04", "2026-10-06"]
    assert sorted(p["name"] for p in d["projects"]) == ["p1", "p2"]
    assert d["total_sessions"] == 4
    assert d["alias"] == "first"
    assert d["today_hourly_date"] == "2026-10-06"


def test_legacy_email_query_and_payload(client):
    client.post("/api/sync", json=payload("old-box", keys=[], email="me@example.com"))
    assert [d["hostname"] for d in client.get("/api/usage", params={"email": "me@example.com"}).json()] == ["old-box"]


def test_oversized_payload_is_rejected_and_not_stored(client):
    big = payload(projects=[{"name": f"p{i}", "session_count": 1} for i in range(100_000)])
    assert len(json.dumps(big)) > app_module.DEFAULT_MAX_BODY_BYTES
    r = client.post("/api/sync", json=big)
    assert r.status_code == 413
    assert "byte limit" in r.json()["detail"]
    assert client.get("/api/usage", params={"hostname": "box-a"}).json() == []
    # the server keeps working afterwards
    assert client.post("/api/sync", json=payload()).status_code == 200


def test_body_limit_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("CMU_MAX_BODY_BYTES", "200")
    with _make_client(tmp_path, monkeypatch) as c:
        assert c.post("/api/sync", json={"hostname": "tiny", "synced_at": "x"}).status_code == 200
        assert c.post("/api/sync", json=payload()).status_code == 413


def test_chunked_body_without_content_length_is_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("CMU_MAX_BODY_BYTES", "200")
    body = json.dumps(payload()).encode()
    with _make_client(tmp_path, monkeypatch) as c:
        r = c.post("/api/sync", content=iter([body[:100], body[100:]]),
                   headers={"Content-Type": "application/json"})
        assert "content-length" not in {k.lower() for k in r.request.headers}
        assert r.status_code == 413


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_invalid_body_limit_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("CMU_MAX_BODY_BYTES", raw)
    with pytest.raises(ValueError):
        app_module.get_max_body_bytes()


def test_default_body_limit(monkeypatch):
    monkeypatch.delenv("CMU_MAX_BODY_BYTES", raising=False)
    assert app_module.get_max_body_bytes() == app_module.DEFAULT_MAX_BODY_BYTES


def test_store_direct_roundtrip(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.upsert_device(SyncPayload(hostname="old", synced_at="x", email="me@example.com"))
    assert store.get_device_data("old").keys == ["me@example.com"]
    assert store.get_device_data("missing") is None
    assert [d.hostname for d in store.list_devices()] == ["old"]
    store.close()
