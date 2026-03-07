"""SQLite-based storage for the sync server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import (
    SyncPayload,
    DeviceInfo,
    DeviceActivity,
    DeviceModelTokens,
    DeviceProject,
)

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
                last_synced TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_keys (
                hostname TEXT NOT NULL,
                key TEXT NOT NULL,
                PRIMARY KEY (hostname, key),
                FOREIGN KEY (hostname) REFERENCES devices(hostname) ON DELETE CASCADE
            );
        """)
        # alias 컬럼 마이그레이션
        try:
            self._conn.execute("SELECT alias FROM devices LIMIT 0")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE devices ADD COLUMN alias TEXT")
        # email → key 마이그레이션: 기존 email 컬럼 데이터를 device_keys로 이동
        try:
            rows = self._conn.execute(
                "SELECT hostname, email FROM devices WHERE email IS NOT NULL AND email != ''"
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "INSERT OR IGNORE INTO device_keys (hostname, key) VALUES (?, ?)",
                    (row["hostname"], row["email"]),
                )
            if rows:
                self._conn.commit()
        except sqlite3.OperationalError:
            pass  # email 컬럼이 없는 경우 무시
        self._conn.commit()

    def upsert_device(self, payload: SyncPayload) -> None:
        existing = self.get_device_data(payload.hostname)
        if existing is not None:
            payload = _merge_payloads(existing, payload)

        self._conn.execute(
            """INSERT INTO devices (hostname, last_synced, data, alias)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(hostname)
               DO UPDATE SET last_synced = excluded.last_synced,
                             data = excluded.data,
                             alias = excluded.alias""",
            (payload.hostname, payload.synced_at,
             payload.model_dump_json(), payload.alias),
        )
        # device_keys 업데이트
        if payload.keys:
            self._conn.execute(
                "DELETE FROM device_keys WHERE hostname = ?",
                (payload.hostname,),
            )
            for key in payload.keys:
                self._conn.execute(
                    "INSERT OR IGNORE INTO device_keys (hostname, key) VALUES (?, ?)",
                    (payload.hostname, key),
                )
        self._conn.commit()

    def list_devices(self, key: str | None = None) -> list[DeviceInfo]:
        if key:
            rows = self._conn.execute(
                """SELECT d.hostname, d.last_synced, d.data, d.alias
                   FROM devices d
                   JOIN device_keys dk ON d.hostname = dk.hostname
                   WHERE dk.key = ?""",
                (key,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT hostname, last_synced, data, alias FROM devices"
            ).fetchall()
        devices = []
        for row in rows:
            data = json.loads(row["data"])
            key_rows = self._conn.execute(
                "SELECT key FROM device_keys WHERE hostname = ?",
                (row["hostname"],),
            ).fetchall()
            keys = [kr["key"] for kr in key_rows]
            devices.append(DeviceInfo(
                hostname=row["hostname"],
                alias=row["alias"],
                keys=keys,
                last_synced=row["last_synced"],
                total_sessions=data.get("total_sessions", 0),
                total_messages=data.get("total_messages", 0),
            ))
        return devices

    def get_device_data(self, hostname: str) -> SyncPayload | None:
        row = self._conn.execute(
            "SELECT data, alias FROM devices WHERE hostname = ?", (hostname,)
        ).fetchone()
        if not row:
            return None
        payload = SyncPayload.model_validate_json(row["data"])
        payload.alias = row["alias"]
        key_rows = self._conn.execute(
            "SELECT key FROM device_keys WHERE hostname = ?",
            (hostname,),
        ).fetchall()
        payload.keys = [kr["key"] for kr in key_rows]
        return payload

    def get_all_data(self, key: str | None = None) -> list[SyncPayload]:
        if key:
            rows = self._conn.execute(
                """SELECT d.data, d.hostname, d.alias
                   FROM devices d
                   JOIN device_keys dk ON d.hostname = dk.hostname
                   WHERE dk.key = ?""",
                (key,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data, hostname, alias FROM devices"
            ).fetchall()
        results = []
        for row in rows:
            payload = SyncPayload.model_validate_json(row["data"])
            payload.alias = row["alias"]
            key_rows = self._conn.execute(
                "SELECT key FROM device_keys WHERE hostname = ?",
                (row["hostname"],),
            ).fetchall()
            payload.keys = [kr["key"] for kr in key_rows]
            results.append(payload)
        return results

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# Private merge helpers
# ---------------------------------------------------------------------------

def _merge_payloads(old: SyncPayload, new: SyncPayload) -> SyncPayload:
    """Merge *new* payload into *old*, preserving historical data."""
    merged_keys = list(set(old.keys + new.keys))
    return SyncPayload(
        hostname=new.hostname,
        synced_at=new.synced_at,
        alias=new.alias or old.alias,
        keys=merged_keys,
        daily_activity=_merge_daily_activity(old.daily_activity, new.daily_activity),
        daily_model_tokens=_merge_daily_model_tokens(
            old.daily_model_tokens, new.daily_model_tokens
        ),
        # 누적 데이터: 새 payload에 있으면 사용, 없으면 기존 유지 (하위호환)
        model_usage=new.model_usage or old.model_usage,
        projects=_merge_projects(old.projects, new.projects),
        hour_counts=new.hour_counts or old.hour_counts,
        total_sessions=max(old.total_sessions, new.total_sessions),
        total_messages=max(old.total_messages, new.total_messages),
        first_session_date=_earlier_date(old.first_session_date, new.first_session_date),
    )


def _merge_daily_activity(
    old: list[DeviceActivity], new: list[DeviceActivity]
) -> list[DeviceActivity]:
    by_date: dict[str, DeviceActivity] = {a.date: a for a in old}
    for a in new:
        by_date[a.date] = a
    return sorted(by_date.values(), key=lambda a: a.date)


def _merge_daily_model_tokens(
    old: list[DeviceModelTokens], new: list[DeviceModelTokens]
) -> list[DeviceModelTokens]:
    by_date: dict[str, DeviceModelTokens] = {t.date: t for t in old}
    for t in new:
        by_date[t.date] = t
    return sorted(by_date.values(), key=lambda t: t.date)



def _merge_projects(
    old: list[DeviceProject], new: list[DeviceProject]
) -> list[DeviceProject]:
    by_name: dict[str, DeviceProject] = {p.name: p for p in old}
    for p in new:
        if p.name in by_name:
            prev = by_name[p.name]
            by_name[p.name] = DeviceProject(
                name=p.name,
                session_count=max(prev.session_count, p.session_count),
                output_tokens=max(prev.output_tokens, p.output_tokens),
                input_tokens=max(prev.input_tokens, p.input_tokens),
                first_seen=_earlier_date(prev.first_seen, p.first_seen),
                last_seen=_later_date(prev.last_seen, p.last_seen),
            )
        else:
            by_name[p.name] = p
    return list(by_name.values())



def _earlier_date(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _later_date(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
