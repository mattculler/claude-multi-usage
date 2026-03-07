"""FastAPI server for collecting usage data from multiple devices."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException

from .models import SyncPayload, DeviceInfo
from .store import Store

_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        db_path = os.environ.get("CMU_DB_PATH", str(Path("/data") / "server.db"))
        _store = Store(db_path=db_path)
    return _store


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_store()
    yield
    if _store is not None:
        _store.close()


app = FastAPI(title="Claude Multi Usage Server", lifespan=lifespan)


@app.post("/api/sync")
async def sync(payload: SyncPayload):
    """Receive usage data from a device."""
    get_store().upsert_device(payload)
    return {"status": "ok", "hostname": payload.hostname}


@app.get("/api/devices", response_model=list[DeviceInfo])
async def list_devices(
    key: str | None = Query(None, description="Filter by key"),
    # backward compat
    email: str | None = Query(None, description="(deprecated) Filter by email, use key instead"),
):
    """List all registered devices, optionally filtered by key."""
    filter_key = key or email
    return get_store().list_devices(key=filter_key)


@app.get("/api/usage")
async def get_usage(
    key: str | None = Query(None, description="Filter by key"),
    hostname: str | None = Query(None, description="Filter by hostname"),
    # backward compat
    email: str | None = Query(None, description="(deprecated) Filter by email, use key instead"),
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
