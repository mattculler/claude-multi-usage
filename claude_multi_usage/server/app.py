"""FastAPI server for collecting usage data from multiple devices."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from .models import SyncPayload, DeviceInfo
from .store import Store

_store: Store | None = None

# Largest request body the server accepts. A full sync payload for a heavy
# user is a few hundred KB, so anything bigger is a mistake or abuse.
# Override with CMU_MAX_BODY_BYTES or `cmu server start --max-body-bytes`.
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024


def get_store() -> Store:
    global _store
    if _store is None:
        db_path = os.environ.get("CMU_DB_PATH", str(Path("/data") / "server.db"))
        _store = Store(db_path=db_path)
    return _store


def get_max_body_bytes() -> int:
    raw = os.environ.get("CMU_MAX_BODY_BYTES")
    if not raw:
        return DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"CMU_MAX_BODY_BYTES must be an integer, got {raw!r}")
    if value <= 0:
        raise ValueError("CMU_MAX_BODY_BYTES must be positive")
    return value


class BodySizeLimitMiddleware:
    """Reject request bodies larger than the configured limit with HTTP 413.

    A Content-Length above the limit is rejected before any of the body is
    read. Bodies sent without Content-Length (chunked) are counted as they
    arrive and rejected once they exceed the limit; FastAPI re-raises the
    HTTPException raised from the body read, so the client still gets a 413.
    The limit is read per request so it can be configured via the
    environment before the app module is imported or afterwards.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = get_max_body_bytes()
        detail = f"Request body exceeds the {max_bytes} byte limit"

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > max_bytes:
                    await self._reject(send, detail)
                    return
                break

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise HTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _reject(send, detail: str):
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_max_body_bytes()  # fail at startup on a bad CMU_MAX_BODY_BYTES
    get_store()
    yield
    if _store is not None:
        _store.close()


app = FastAPI(title="Claude Multi Usage Server", lifespan=lifespan)
app.add_middleware(BodySizeLimitMiddleware)


@app.post("/api/sync")
async def sync(payload: SyncPayload):
    """Receive usage data from a device."""
    get_store().upsert_device(payload)
    return {"status": "ok", "hostname": payload.hostname}


@app.get("/api/devices", response_model=list[DeviceInfo])
async def list_devices(
    key: Optional[str] = Query(None, description="Filter by key"),
    # backward compat
    email: Optional[str] = Query(None, description="(deprecated) Filter by email, use key instead"),
):
    """List all registered devices, optionally filtered by key."""
    filter_key = key or email
    return get_store().list_devices(key=filter_key)


@app.get("/api/usage")
async def get_usage(
    key: Optional[str] = Query(None, description="Filter by key"),
    hostname: Optional[str] = Query(None, description="Filter by hostname"),
    # backward compat
    email: Optional[str] = Query(None, description="(deprecated) Filter by email, use key instead"),
):
    """Get usage data, filtered by key or hostname."""
    store = get_store()
    if hostname:
        data = store.get_device_data(hostname)
        return [data] if data else []
    filter_key = key or email
    return store.get_all_data(key=filter_key)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
