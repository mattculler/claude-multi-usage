"""SQLite-based storage for the sync server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import SyncPayload, DeviceInfo

DEFAULT_DB_PATH = Path("/data") / "server.db"


class Store:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS devices (
                hostname TEXT PRIMARY KEY,
                email TEXT,
                last_synced TEXT NOT NULL,
                data TEXT NOT NULL
            );
        """)
        # 기존 테이블에 email 컬럼이 없으면 추가 (마이그레이션)
        try:
            self._conn.execute("SELECT email FROM devices LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE devices ADD COLUMN email TEXT")
        self._conn.commit()

    def upsert_device(self, payload: SyncPayload) -> None:
        self._conn.execute(
            """INSERT INTO devices (hostname, email, last_synced, data)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(hostname)
               DO UPDATE SET email = excluded.email,
                             last_synced = excluded.last_synced,
                             data = excluded.data""",
            (payload.hostname, payload.email, payload.synced_at,
             payload.model_dump_json()),
        )
        self._conn.commit()

    def list_devices(self, email: str | None = None) -> list[DeviceInfo]:
        if email:
            rows = self._conn.execute(
                "SELECT hostname, email, last_synced, data FROM devices WHERE email = ?",
                (email,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT hostname, email, last_synced, data FROM devices"
            ).fetchall()
        devices = []
        for row in rows:
            data = json.loads(row["data"])
            devices.append(DeviceInfo(
                hostname=row["hostname"],
                email=row["email"],
                last_synced=row["last_synced"],
                total_sessions=data.get("total_sessions", 0),
                total_messages=data.get("total_messages", 0),
            ))
        return devices

    def get_device_data(self, hostname: str) -> SyncPayload | None:
        row = self._conn.execute(
            "SELECT data FROM devices WHERE hostname = ?", (hostname,)
        ).fetchone()
        if not row:
            return None
        return SyncPayload.model_validate_json(row["data"])

    def get_all_data(self, email: str | None = None) -> list[SyncPayload]:
        if email:
            rows = self._conn.execute(
                "SELECT data FROM devices WHERE email = ?", (email,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT data FROM devices").fetchall()
        return [SyncPayload.model_validate_json(row["data"]) for row in rows]

    def close(self):
        self._conn.close()
