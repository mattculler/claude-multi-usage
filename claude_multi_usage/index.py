"""Incremental index of Claude Code session logs.

Everything cmu shows is derived from ``~/.claude/projects/**/*.jsonl``.
Those files are append-only and large (a single long session can be tens
of megabytes), so parsing all of them on every command does not scale.

This module keeps a SQLite index in ``~/.claude-multi-usage/index.db``:

* A file is re-read only when its size or mtime changed. When it grew and
  the bytes before the old end are unchanged (the normal case for the
  session in use) only the new bytes are read; otherwise it is re-read in
  full.
* A file that disappeared (Claude Code deletes old transcripts after its
  retention period) is marked gone and its rows are kept, so cost history
  does not roll off with the transcripts. It is re-read if it reappears.
* One row per assistant message *occurrence*. Claude Code writes one JSONL
  line per content block, repeating the message id; the first line carries
  a placeholder output count and later lines the final one, so the maximum
  per id is kept. Resumed sessions copy earlier history into a new file;
  aggregations therefore count each message id once (the ``owned`` view).
* Token counts are stored, never prices, so a changed pricing table or a
  fix in the cost logic takes effect immediately without any rebuild.
* Refreshes run in one write transaction, so concurrent cmu processes
  (the README's shell wrapper backgrounds ``cmu sync``) serialize instead
  of double counting.

Local dates and hours are computed when a file is indexed; after changing
the system time zone run ``cmu cost --rebuild``. If the index cannot be
opened (read-only home directory) an in-memory index is used for the run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import parser as _parser
from .parser import (
    DailyActivity,
    DailyModelTokens,
    HourlyUsage,
    ProjectSummary,
    _project_name_from_dir,
    _repo_name_from_cwd,
    _utc_to_local,
)

CACHE_DIR = Path.home() / ".claude-multi-usage"
INDEX_FILE = CACHE_DIR / "index.db"
SCHEMA_VERSION = 2
_TAIL_BYTES = 256  # bytes remembered from the end of the indexed region

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    offset      INTEGER NOT NULL,       -- bytes indexed so far
    tail        BLOB,                   -- last bytes before offset, to detect rewrites
    gone        INTEGER NOT NULL DEFAULT 0,  -- no longer on disk; rows kept as history
    project_dir TEXT NOT NULL,          -- name of the ~/.claude/projects/<dir>
    is_subagent INTEGER NOT NULL,       -- nested under a session directory
    cwd         TEXT,
    project     TEXT NOT NULL,
    first_ts    TEXT,                   -- naive UTC ISO, comparable as strings
    last_ts     TEXT,
    ts_source   TEXT NOT NULL DEFAULT 'log'  -- 'log' or 'mtime' (no timestamped entries yet)
);
CREATE TABLE IF NOT EXISTS messages (
    path         TEXT NOT NULL,
    msg_id       TEXT NOT NULL,
    model        TEXT NOT NULL,
    date         TEXT NOT NULL,         -- local YYYY-MM-DD
    hour         INTEGER NOT NULL,      -- local 0-23
    input        INTEGER NOT NULL,
    output       INTEGER NOT NULL,
    cache_read   INTEGER NOT NULL,
    cache_create INTEGER NOT NULL,
    PRIMARY KEY (path, msg_id)
);
CREATE INDEX IF NOT EXISTS messages_date ON messages (date);
CREATE INDEX IF NOT EXISTS messages_msg ON messages (msg_id);
CREATE TABLE IF NOT EXISTS lines (
    path            TEXT NOT NULL,
    date            TEXT NOT NULL,
    hour            INTEGER NOT NULL,
    user_lines      INTEGER NOT NULL,
    assistant_lines INTEGER NOT NULL,
    tool_calls      INTEGER NOT NULL,
    PRIMARY KEY (path, date, hour)
);
CREATE INDEX IF NOT EXISTS lines_date ON lines (date);
-- Each message id counted once, attributed to one file.
CREATE VIEW IF NOT EXISTS owned AS
    SELECT m.* FROM messages m
    JOIN (SELECT msg_id, MIN(path) AS path FROM messages GROUP BY msg_id) o
      ON o.msg_id = m.msg_id AND o.path = m.path;
"""

_TABLES = ("owned", "lines", "messages", "files", "meta")


@dataclass
class RefreshStats:
    scanned: int = 0      # session files on disk
    indexed: int = 0      # files read in full
    appended: int = 0     # files read from their previous offset
    retired: int = 0      # files that disappeared (rows kept)


def _utc_iso(ts: str) -> str | None:
    """Normalize a Claude Code timestamp to naive-UTC ISO for string comparison."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _from_utc_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class SessionIndex:
    def __init__(self, db_path: Path | str = INDEX_FILE):
        self.db_path = Path(db_path)
        self.fallback_reason: str | None = None
        conn = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=60)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except (OSError, sqlite3.Error) as e:
            if conn is not None:
                conn.close()
            # Read-only or otherwise unusable location: index in memory for this run.
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            self.fallback_reason = f"{self.db_path}: {e}"
        self._conn = conn
        self._ensure_schema()

    # -- schema -----------------------------------------------------------

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None and row["value"] != str(SCHEMA_VERSION):
            # An older index: start over rather than guess at a migration.
            self._conn.executescript(
                "DROP VIEW IF EXISTS owned;" + "".join(f"DROP TABLE IF EXISTS {t};" for t in _TABLES[1:])
            )
            self._conn.executescript(_SCHEMA)
            row = None
        if row is None:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- refresh ----------------------------------------------------------

    def _scan(self) -> dict[str, tuple[int, float, str, bool]]:
        projects_dir = _parser.PROJECTS_DIR
        on_disk: dict[str, tuple[int, float, str, bool]] = {}
        if not projects_dir.exists():
            return on_disk
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for f in project_dir.rglob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                on_disk[str(f)] = (st.st_size, st.st_mtime, project_dir.name, f.parent != project_dir)
        return on_disk

    def refresh(self) -> RefreshStats:
        """Bring the index up to date with the files on disk."""
        stats = RefreshStats()
        on_disk = self._scan()
        stats.scanned = len(on_disk)

        # One write transaction: a second process refreshing at the same time
        # waits here and then sees the updated offsets instead of re-adding
        # the same appended lines.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            known = {
                row["path"]: row
                for row in self._conn.execute("SELECT path, size, mtime, offset, tail, gone FROM files")
            }

            for path, row in known.items():
                if path not in on_disk and not row["gone"]:
                    self._conn.execute("UPDATE files SET gone = 1 WHERE path = ?", (path,))
                    stats.retired += 1

            for path, (size, mtime, project_dir_name, is_subagent) in on_disk.items():
                row = known.get(path)
                if row is not None and not row["gone"]:
                    if row["size"] == size and row["mtime"] == mtime:
                        continue
                    if size >= row["offset"] and self._prefix_unchanged(path, row["offset"], row["tail"]):
                        self._index_file(path, size, mtime, project_dir_name, is_subagent,
                                         offset=row["offset"])
                        stats.appended += 1
                        continue
                self._forget(path)
                self._index_file(path, size, mtime, project_dir_name, is_subagent, offset=0)
                stats.indexed += 1

            self._inherit_projects()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return stats

    def reindex_present(self) -> RefreshStats:
        """Re-read every file on disk; rows of files that are gone are kept."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM messages WHERE path IN (SELECT path FROM files WHERE gone = 0)")
            self._conn.execute("DELETE FROM lines WHERE path IN (SELECT path FROM files WHERE gone = 0)")
            self._conn.execute("DELETE FROM files WHERE gone = 0")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.refresh()

    @staticmethod
    def _prefix_unchanged(path: str, offset: int, tail: bytes | None) -> bool:
        """True if the bytes just before ``offset`` are what was indexed last time."""
        if offset == 0:
            return True
        if not tail:
            return False
        try:
            with open(path, "rb") as f:
                f.seek(max(0, offset - len(tail)))
                return f.read(len(tail)) == tail
        except OSError:
            return False

    def _forget(self, path: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE path = ?", (path,))
        self._conn.execute("DELETE FROM lines WHERE path = ?", (path,))
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def _inherit_projects(self) -> None:
        # A file that recorded no cwd (some subagent transcripts) belongs to
        # the same project as its siblings in the directory; only when no
        # sibling knows either does the directory-name guess stand.
        self._conn.execute(
            """UPDATE files SET project = (
                   SELECT s.project FROM files s
                   WHERE s.project_dir = files.project_dir AND s.cwd IS NOT NULL
                   ORDER BY s.is_subagent, s.path LIMIT 1)
               WHERE cwd IS NULL AND EXISTS (
                   SELECT 1 FROM files s
                   WHERE s.project_dir = files.project_dir AND s.cwd IS NOT NULL)"""
        )

    def _index_file(self, path: str, size: int, mtime: float, project_dir_name: str,
                    is_subagent: bool, offset: int) -> None:
        existing = self._conn.execute(
            "SELECT cwd, first_ts, last_ts, ts_source FROM files WHERE path = ?", (path,)
        ).fetchone() if offset else None
        cwd = existing["cwd"] if existing else None
        first_ts = last_ts = None
        if existing and existing["ts_source"] == "log":
            first_ts, last_ts = existing["first_ts"], existing["last_ts"]

        line_counts: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        message_rows: list[tuple] = []
        new_offset = offset
        tail = b""

        try:
            with open(path, "rb") as f:
                f.seek(offset)
                line_no = 0
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    try:
                        d = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        if not raw.endswith(b"\n"):
                            break  # mid-write; pick it up next time
                        new_offset += len(raw)
                        line_no += 1
                        continue
                    # A complete JSON line counts even without a trailing newline.
                    new_offset += len(raw)
                    line_no += 1
                    if not isinstance(d, dict):
                        continue

                    if cwd is None:
                        c = d.get("cwd")
                        if isinstance(c, str) and c:
                            cwd = c

                    ts = d.get("timestamp")
                    ts_utc = _utc_iso(ts) if isinstance(ts, str) else None
                    if ts_utc:
                        if first_ts is None or ts_utc < first_ts:
                            first_ts = ts_utc
                        if last_ts is None or ts_utc > last_ts:
                            last_ts = ts_utc

                    entry_type = d.get("type")
                    if entry_type == "user":
                        local = _utc_to_local(ts) if isinstance(ts, str) else None
                        if local is not None:
                            line_counts[(local.strftime("%Y-%m-%d"), local.hour)][0] += 1
                    elif entry_type == "assistant":
                        message = d.get("message")
                        local = _utc_to_local(ts) if isinstance(ts, str) else None
                        if local is None or not isinstance(message, dict):
                            continue
                        key = (local.strftime("%Y-%m-%d"), local.hour)
                        counts = line_counts[key]
                        counts[1] += 1
                        content = message.get("content")
                        if isinstance(content, list):
                            counts[2] += sum(
                                1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use"
                            )
                        message_rows.append(self._message_row(path, message, key, f"{offset}:{line_no}"))
                    elif entry_type == "progress":
                        # Older Claude Code versions logged subagent responses as
                        # progress entries wrapping a message.
                        data = d.get("data")
                        inner = data.get("message") if isinstance(data, dict) else None
                        message = inner.get("message") if isinstance(inner, dict) else None
                        inner_ts = inner.get("timestamp") if isinstance(inner, dict) else None
                        local = _utc_to_local(inner_ts) if isinstance(inner_ts, str) else None
                        if local is None or not isinstance(message, dict):
                            continue
                        key = (local.strftime("%Y-%m-%d"), local.hour)
                        message_rows.append(self._message_row(path, message, key, f"{offset}:{line_no}"))

                start = max(0, new_offset - _TAIL_BYTES)
                f.seek(start)
                tail = f.read(new_offset - start)
        except OSError:
            return

        ts_source = "log"
        if first_ts is None:
            # No timestamped entries yet: fall back to the file's mtime until some appear.
            first_ts = last_ts = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            ts_source = "mtime"

        project = _repo_name_from_cwd(cwd) if cwd else _project_name_from_dir(project_dir_name)

        self._conn.execute(
            """INSERT INTO files (path, size, mtime, offset, tail, gone, project_dir, is_subagent,
                                  cwd, project, first_ts, last_ts, ts_source)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 size = excluded.size, mtime = excluded.mtime, offset = excluded.offset,
                 tail = excluded.tail, gone = 0, cwd = excluded.cwd, project = excluded.project,
                 first_ts = excluded.first_ts, last_ts = excluded.last_ts,
                 ts_source = excluded.ts_source""",
            (path, size, mtime, new_offset, tail, project_dir_name, int(is_subagent), cwd, project,
             first_ts, last_ts, ts_source),
        )
        # Later lines of a streamed message carry the final usage; keep the maximum.
        self._conn.executemany(
            """INSERT INTO messages
               (path, msg_id, model, date, hour, input, output, cache_read, cache_create)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path, msg_id) DO UPDATE SET
                 input = MAX(input, excluded.input),
                 output = MAX(output, excluded.output),
                 cache_read = MAX(cache_read, excluded.cache_read),
                 cache_create = MAX(cache_create, excluded.cache_create)""",
            message_rows,
        )
        self._conn.executemany(
            """INSERT INTO lines (path, date, hour, user_lines, assistant_lines, tool_calls)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(path, date, hour) DO UPDATE SET
                 user_lines = user_lines + excluded.user_lines,
                 assistant_lines = assistant_lines + excluded.assistant_lines,
                 tool_calls = tool_calls + excluded.tool_calls""",
            [(path, date, hour, c[0], c[1], c[2]) for (date, hour), c in line_counts.items()],
        )

    @staticmethod
    def _message_row(path: str, message: dict, key: tuple[str, int], fallback_id: str) -> tuple:
        usage = message.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        msg_id = message.get("id") or f"{path}#{fallback_id}"
        model = message.get("model") or "unknown"

        def _int(v):
            return v if isinstance(v, int) and not isinstance(v, bool) else 0

        return (
            path, str(msg_id), str(model), key[0], key[1],
            _int(usage.get("input_tokens")), _int(usage.get("output_tokens")),
            _int(usage.get("cache_read_input_tokens")), _int(usage.get("cache_creation_input_tokens")),
        )

    # -- queries ----------------------------------------------------------

    def message_usage(self, date_from: str | None = None, date_to: str | None = None,
                      by_hour: bool = False) -> Iterator[sqlite3.Row]:
        """Token sums per (date[, hour], model) over messages counted once.

        Rows with ``big`` = 0 are sums of messages that are each below the
        tiered-pricing threshold, so their cost is linear in the sum (price
        them untiered). Messages with any count above the threshold are
        returned individually with ``big`` = 1 so tiered cost is exact.
        """
        from .pricing import TIERED_THRESHOLD as T

        where, params = [], []
        if date_from:
            where.append("date >= ?")
            params.append(date_from)
        if date_to:
            where.append("date <= ?")
            params.append(date_to)
        cond = (" AND " + " AND ".join(where)) if where else ""
        hour_col = "hour" if by_hour else "0 AS hour"
        group = "date, hour, model" if by_hour else "date, model"
        small = self._conn.execute(
            f"""SELECT date, {hour_col}, model,
                       SUM(input) AS input, SUM(output) AS output,
                       SUM(cache_read) AS cache_read, SUM(cache_create) AS cache_create,
                       COUNT(*) AS messages, 0 AS big
                FROM owned
                WHERE input <= ? AND output <= ? AND cache_read <= ? AND cache_create <= ?{cond}
                GROUP BY {group}""",
            [T, T, T, T, *params],
        ).fetchall()
        large = self._conn.execute(
            f"""SELECT date, {hour_col}, model, input, output, cache_read, cache_create,
                       1 AS messages, 1 AS big
                FROM owned
                WHERE (input > ? OR output > ? OR cache_read > ? OR cache_create > ?){cond}""",
            [T, T, T, T, *params],
        ).fetchall()
        yield from small
        yield from large

    def hourly(self, date: str) -> list[HourlyUsage]:
        """Per-hour usage for one local day (messages, sessions, output tokens, cost)."""
        from .pricing import get_pricing, usage_cost

        has_pricing = get_pricing() is not None
        hours = {h: {"messages": 0, "sessions": set(), "tokens": 0, "cost": 0.0} for h in range(24)}
        for row in self._conn.execute(
            """SELECT l.hour, l.user_lines + l.assistant_lines AS msgs, l.path
               FROM lines l JOIN files f ON f.path = l.path
               WHERE l.date = ? AND f.is_subagent = 0""",
            (date,),
        ):
            hours[row["hour"]]["messages"] += row["msgs"]
            hours[row["hour"]]["sessions"].add(row["path"])
        for row in self.message_usage(date, date, by_hour=True):
            h = hours[row["hour"]]
            h["tokens"] += row["output"]
            if has_pricing:
                h["cost"] += usage_cost(row)
        return [
            HourlyUsage(hour=h, message_count=v["messages"], session_count=len(v["sessions"]),
                        tokens=v["tokens"], cost=v["cost"])
            for h, v in sorted(hours.items())
        ]

    def day_usage(self, date: str) -> Optional[tuple[DailyActivity, DailyModelTokens]]:
        """Messages, sessions, tool calls and output tokens by model for one local day."""
        row = self._conn.execute(
            """SELECT COALESCE(SUM(l.user_lines + l.assistant_lines), 0) AS messages,
                      COALESCE(SUM(l.tool_calls), 0) AS tool_calls,
                      COUNT(DISTINCT CASE WHEN l.user_lines > 0 THEN l.path END) AS sessions
               FROM lines l JOIN files f ON f.path = l.path
               WHERE l.date = ? AND f.is_subagent = 0""",
            (date,),
        ).fetchone()
        tokens: dict[str, int] = {}
        for r in self.message_usage(date, date):
            tokens[r["model"]] = tokens.get(r["model"], 0) + r["output"]
        if row["messages"] == 0 and not tokens:
            return None
        return (
            DailyActivity(date=date, message_count=row["messages"],
                          session_count=row["sessions"], tool_call_count=row["tool_calls"]),
            DailyModelTokens(date=date, tokens_by_model=tokens),
        )

    def projects(self) -> list[ProjectSummary]:
        """Per-repository summaries: sessions, tokens, cost, first/last seen."""
        from .pricing import TIERED_THRESHOLD as T, get_pricing, usage_cost

        has_pricing = get_pricing() is not None
        summaries: dict[str, ProjectSummary] = {}
        for row in self._conn.execute(
            """SELECT project,
                      SUM(CASE WHEN is_subagent = 0 THEN 1 ELSE 0 END) AS sessions,
                      MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts
               FROM files GROUP BY project"""
        ):
            summaries[row["project"]] = ProjectSummary(
                name=row["project"], session_count=row["sessions"],
                first_seen=_from_utc_iso(row["first_ts"]), last_seen=_from_utc_iso(row["last_ts"]),
            )
        for row in self._conn.execute(
            """SELECT f.project, o.model,
                      SUM(o.input) AS input, SUM(o.output) AS output,
                      SUM(o.cache_read) AS cache_read, SUM(o.cache_create) AS cache_create, 0 AS big
               FROM owned o JOIN files f ON f.path = o.path
               WHERE o.input <= ? AND o.output <= ? AND o.cache_read <= ? AND o.cache_create <= ?
               GROUP BY f.project, o.model
               UNION ALL
               SELECT f.project, o.model, o.input, o.output, o.cache_read, o.cache_create, 1 AS big
               FROM owned o JOIN files f ON f.path = o.path
               WHERE o.input > ? OR o.output > ? OR o.cache_read > ? OR o.cache_create > ?""",
            (T, T, T, T, T, T, T, T),
        ):
            s = summaries.get(row["project"])
            if s is None:
                continue
            s.output_tokens += row["output"]
            s.input_tokens += row["input"] + row["cache_read"] + row["cache_create"]
            if has_pricing:
                s.cost += usage_cost(row)
        return sorted(summaries.values(), key=lambda p: p.output_tokens, reverse=True)

    def file_count(self, include_gone: bool = True) -> int:
        if include_gone:
            return self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM files WHERE gone = 0").fetchone()[0]


# -- process-wide instance -------------------------------------------------

_instance: SessionIndex | None = None


def get_index() -> SessionIndex:
    """The index for this process, refreshed once on first use."""
    global _instance
    if _instance is None:
        idx = SessionIndex(INDEX_FILE)
        if idx.fallback_reason:
            print(f"cmu: session index unavailable ({idx.fallback_reason}); "
                  "reading session files directly this run", file=sys.stderr)
        idx.refresh()
        _instance = idx
    return _instance


def rebuild() -> SessionIndex:
    """Re-read every session file on disk (e.g. after a time zone change).

    Rows of transcripts that Claude Code has already deleted are kept as
    they are, since there is nothing left to re-read them from.
    """
    global _instance
    if _instance is None:
        _instance = SessionIndex(INDEX_FILE)
    _instance.reindex_present()
    return _instance
