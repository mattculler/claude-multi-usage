"""Parse Claude Code local data from ~/.claude"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


CLAUDE_DIR = Path.home() / ".claude"
STATS_FILE = CLAUDE_DIR / "stats-cache.json"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def _utc_to_local(ts: str) -> datetime | None:
    """Convert UTC timestamp string (with Z or +00:00) to local datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone()
    except (ValueError, AttributeError):
        return None


@dataclass
class DailyActivity:
    date: str
    message_count: int
    session_count: int
    tool_call_count: int


@dataclass
class DailyModelTokens:
    date: str
    tokens_by_model: dict[str, int]

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens_by_model.values())


@dataclass
class ModelUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass
class ProjectSummary:
    name: str
    session_count: int
    output_tokens: int = 0
    input_tokens: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    cost: float = 0.0


@dataclass
class HourlyUsage:
    hour: int
    message_count: int
    session_count: int
    tokens: int
    cost: float = 0.0


@dataclass
class UsageData:
    hostname: str
    alias: str | None = None
    daily_activity: list[DailyActivity] = field(default_factory=list)
    daily_model_tokens: list[DailyModelTokens] = field(default_factory=list)
    model_usage: list[ModelUsage] = field(default_factory=list)
    projects: list[ProjectSummary] = field(default_factory=list)
    hour_counts: dict[int, int] = field(default_factory=dict)
    total_sessions: int = 0
    total_messages: int = 0
    first_session_date: str | None = None
    # Per-hour usage for one day, only populated for data received from the
    # sync server; today_hourly_date says which day it describes.
    today_hourly: list[HourlyUsage] = field(default_factory=list)
    today_hourly_date: str | None = None


def get_hostname() -> str:
    return socket.gethostname()


def parse_stats_cache() -> dict | None:
    if not STATS_FILE.exists():
        return None
    with open(STATS_FILE) as f:
        return json.load(f)


def _deduplicate_project_name(name: str) -> str:
    """Remove duplicate path segments from project names.

    e.g., 'apr-backend-assignment-apr-backend-assignment' -> 'apr-backend-assignment'
          'url-jarvis-url-jarvis-docs' -> 'url-jarvis/-docs'
          (the caller then collapses '/-' to '-', giving 'url-jarvis-docs')

    This is a heuristic: any name whose second half starts with its first
    half is treated as a duplicate, so e.g. 'foo-foobar' becomes 'foo/bar'.
    """
    # Split the name at every position; if the remainder starts with
    # "-" + prefix, the prefix was repeated.
    length = len(name)
    for split_pos in range(1, length):
        prefix = name[:split_pos]
        rest = name[split_pos:]
        if rest.startswith("-" + prefix):
            suffix = rest[len(prefix) + 1:]
            if suffix:
                return prefix + "/" + suffix
            return prefix
    return name


def _read_session_cwd(session_file: Path) -> str | None:
    """Return the working directory recorded in a session file, if any.

    Claude Code stamps nearly every entry with the session's ``cwd``, and the
    first such entry is within the first few lines, so this stops early.
    """
    try:
        with open(session_file) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = d.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except (OSError, IOError):
        pass
    return None


_WORKTREE_MARKER = (".claude", "worktrees")


def _repo_name_from_cwd(cwd: str) -> str:
    """Repository name for a session's working directory.

    '/home/me/src/my-repo' -> 'my-repo'. Claude Code worktrees live at
    '<repo>/.claude/worktrees/<name>' and are attributed to the repository,
    not the (often randomly named) worktree. Only this short name is
    displayed and synced; the rest of the path stays on this machine.
    """
    parts = [p for p in re.split(r"[\\/]+", cwd) if p]
    for i in range(len(parts) - 2):
        if (parts[i], parts[i + 1]) == _WORKTREE_MARKER:
            parts = parts[:i]
            break
    if not parts or cwd.rstrip("\\/") == str(Path.home()):
        return "home"
    return parts[-1]


def _project_name_from_dir(raw_name: str) -> str:
    """Fallback: guess a project name from the encoded directory name.

    Claude Code names project directories after the working directory with
    path separators (and other punctuation) replaced by '-', so the original
    path cannot be recovered reliably. Only used when no session file in the
    directory records a ``cwd``.
    """
    parts = raw_name.split("-")
    try:
        ws_idx = parts.index("workspace")
        path_parts = "-".join(parts[ws_idx + 1:]) if ws_idx + 1 < len(parts) else raw_name
        project_name = _deduplicate_project_name(path_parts)
    except ValueError:
        project_name = raw_name

    # An encoded absolute path ("-home-me-src-thing", or "C--Users-me-thing"
    # on Windows): keep only the last segment rather than exposing the whole
    # path. Hyphenated names get truncated here; this is a guess, used only
    # for logs that recorded no working directory.
    if not project_name or project_name.startswith("-") or re.match(r"^[A-Za-z]--", project_name):
        project_name = raw_name.rsplit("-", 1)[-1] or "home"

    return project_name.replace("/-", "-")


def parse_projects() -> list:
    """Per-repository summaries (sessions, tokens, cost, first/last seen).

    Every session is named after the repository it ran in (the last component
    of the working directory it recorded, see _repo_name_from_cwd) and
    sessions from the same repository are aggregated wherever Claude Code
    stored them, subagent transcripts included. Backed by the incremental
    session index, so only changed files are read.
    """
    from .index import get_index

    return get_index().projects()


def parse_today_hourly() -> list[HourlyUsage]:
    """Today's usage broken down by hour (0-23), from the session index."""
    from .index import get_index

    today_str = datetime.now().strftime("%Y-%m-%d")
    return get_index().hourly(today_str)


def parse_today_usage() -> Optional[tuple]:
    """Today's (DailyActivity, DailyModelTokens) from the session index, or None."""
    from .index import get_index

    today_str = datetime.now().strftime("%Y-%m-%d")
    return get_index().day_usage(today_str)


def load_usage_data() -> UsageData:
    """Load all usage data from local Claude Code files."""
    hostname = get_hostname()
    # stats-cache.json is written by Claude Code itself and may be absent
    # (fresh install, cloud container); the session files still exist, so
    # projects and today's figures are reported regardless.
    stats = parse_stats_cache() or {}

    daily_activity = [
        DailyActivity(
            date=d["date"],
            message_count=d["messageCount"],
            session_count=d["sessionCount"],
            tool_call_count=d["toolCallCount"],
        )
        for d in stats.get("dailyActivity", [])
    ]

    daily_model_tokens = [
        DailyModelTokens(
            date=d["date"],
            tokens_by_model=d["tokensByModel"],
        )
        for d in stats.get("dailyModelTokens", [])
    ]

    model_usage = [
        ModelUsage(
            model=model,
            input_tokens=usage["inputTokens"],
            output_tokens=usage["outputTokens"],
            cache_read_tokens=usage["cacheReadInputTokens"],
            cache_creation_tokens=usage["cacheCreationInputTokens"],
        )
        for model, usage in stats.get("modelUsage", {}).items()
    ]

    hour_counts = {int(k): v for k, v in stats.get("hourCounts", {}).items()}
    projects = parse_projects()

    return UsageData(
        hostname=hostname,
        daily_activity=daily_activity,
        daily_model_tokens=daily_model_tokens,
        model_usage=model_usage,
        projects=projects,
        hour_counts=hour_counts,
        total_sessions=stats.get("totalSessions", 0),
        total_messages=stats.get("totalMessages", 0),
        first_session_date=stats.get("firstSessionDate"),
    )
