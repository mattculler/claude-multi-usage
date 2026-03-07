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
    DeviceModelUsage,
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
        existing = self.get_device_data(payload.hostname)
        if existing is not None:
            payload = _merge_payloads(existing, payload)

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


# ---------------------------------------------------------------------------
# Private merge helpers
# ---------------------------------------------------------------------------

def _merge_payloads(old: SyncPayload, new: SyncPayload) -> SyncPayload:
    """Merge *new* payload into *old*, preserving historical data."""
    return SyncPayload(
        hostname=new.hostname,
        synced_at=new.synced_at,
        email=new.email,
        daily_activity=_merge_daily_activity(old.daily_activity, new.daily_activity),
        daily_model_tokens=_merge_daily_model_tokens(
            old.daily_model_tokens, new.daily_model_tokens
        ),
        model_usage=_merge_model_usage(old.model_usage, new.model_usage),
        projects=_merge_projects(old.projects, new.projects),
        hour_counts=_merge_hour_counts(old.hour_counts, new.hour_counts),
        total_sessions=max(old.total_sessions, new.total_sessions),
        total_messages=max(old.total_messages, new.total_messages),
        first_session_date=_earlier_date(old.first_session_date, new.first_session_date),
    )


def _merge_daily_activity(
    old: list[DeviceActivity], new: list[DeviceActivity]
) -> list[DeviceActivity]:
    """날짜 기준 머지. 같은 날짜면 새 데이터 우선, 서버에만 있는 과거 날짜는 유지."""
    by_date: dict[str, DeviceActivity] = {a.date: a for a in old}
    for a in new:
        by_date[a.date] = a  # 새 데이터가 우선
    return sorted(by_date.values(), key=lambda a: a.date)


def _merge_daily_model_tokens(
    old: list[DeviceModelTokens], new: list[DeviceModelTokens]
) -> list[DeviceModelTokens]:
    """날짜 기준 머지. 같은 날짜면 새 데이터 우선, 서버에만 있는 과거 날짜는 유지."""
    by_date: dict[str, DeviceModelTokens] = {t.date: t for t in old}
    for t in new:
        by_date[t.date] = t
    return sorted(by_date.values(), key=lambda t: t.date)


def _merge_model_usage(
    old: list[DeviceModelUsage], new: list[DeviceModelUsage]
) -> list[DeviceModelUsage]:
    """모델별로 각 토큰 필드의 더 큰 값 유지."""
    by_model: dict[str, DeviceModelUsage] = {u.model: u for u in old}
    for u in new:
        if u.model in by_model:
            prev = by_model[u.model]
            by_model[u.model] = DeviceModelUsage(
                model=u.model,
                input_tokens=max(prev.input_tokens, u.input_tokens),
                output_tokens=max(prev.output_tokens, u.output_tokens),
                cache_read_tokens=max(prev.cache_read_tokens, u.cache_read_tokens),
                cache_creation_tokens=max(
                    prev.cache_creation_tokens, u.cache_creation_tokens
                ),
            )
        else:
            by_model[u.model] = u
    return list(by_model.values())


def _merge_projects(
    old: list[DeviceProject], new: list[DeviceProject]
) -> list[DeviceProject]:
    """프로젝트 이름 기준 머지. 수치는 더 큰 값, 날짜는 이른/늦은 값 유지."""
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


def _merge_hour_counts(
    old: dict[str, int], new: dict[str, int]
) -> dict[str, int]:
    """시간대별로 더 큰 값 유지."""
    merged = dict(old)
    for hour, count in new.items():
        merged[hour] = max(merged.get(hour, 0), count)
    return merged


def _earlier_date(a: str | None, b: str | None) -> str | None:
    """두 날짜 중 더 이른 값 반환. None은 무시."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _later_date(a: str | None, b: str | None) -> str | None:
    """두 날짜 중 더 늦은 값 반환. None은 무시."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
