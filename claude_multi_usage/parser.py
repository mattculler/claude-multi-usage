"""Parse Claude Code local data from ~/.claude"""

from __future__ import annotations

import json
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
          'url-jarvis-url-jarvis-docs' -> 'url-jarvis/docs'
    """
    # 이름을 반으로 나눠서 앞뒤가 같으면 중복
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


def _parse_session_tokens(session_file: Path) -> tuple:
    """Parse a session jsonl file and return (output_tokens, input_tokens, timestamps)."""
    output_tokens = 0
    input_tokens = 0
    timestamps = []
    try:
        with open(session_file) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "assistant":
                    usage = d.get("message", {}).get("usage", {})
                    output_tokens += usage.get("output_tokens", 0)
                    input_tokens += usage.get("input_tokens", 0)
                    input_tokens += usage.get("cache_read_input_tokens", 0)
                    input_tokens += usage.get("cache_creation_input_tokens", 0)
                ts = d.get("timestamp")
                if ts:
                    timestamps.append(ts)
    except (OSError, IOError):
        pass
    return output_tokens, input_tokens, timestamps


def parse_projects(with_tokens: bool = True) -> list:
    """Parse project directories to get project summaries."""
    if not PROJECTS_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        raw_name = project_dir.name
        parts = raw_name.split("-")
        try:
            ws_idx = parts.index("workspace")
            path_parts = "-".join(parts[ws_idx + 1:]) if ws_idx + 1 < len(parts) else raw_name
            project_name = _deduplicate_project_name(path_parts)
        except ValueError:
            project_name = raw_name

        if not project_name or project_name.startswith("-Users"):
            project_name = raw_name.rsplit("-", 1)[-1] or "home"

        project_name = project_name.replace("/-", "-")

        if not project_name:
            continue

        session_files = list(project_dir.glob("*.jsonl"))
        session_count = len(session_files)

        total_output = 0
        total_input = 0
        all_timestamps = []

        if with_tokens:
            for sf in session_files:
                out_t, in_t, ts_list = _parse_session_tokens(sf)
                total_output += out_t
                total_input += in_t
                all_timestamps.extend(ts_list)

        first_seen = None
        last_seen = None
        if all_timestamps:
            all_timestamps.sort()
            try:
                first_seen = datetime.fromisoformat(all_timestamps[0].replace("Z", "+00:00"))
                last_seen = datetime.fromisoformat(all_timestamps[-1].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        elif session_files:
            mtimes = [f.stat().st_mtime for f in session_files]
            first_seen = datetime.fromtimestamp(min(mtimes))
            last_seen = datetime.fromtimestamp(max(mtimes))

        projects.append(ProjectSummary(
            name=project_name,
            session_count=session_count,
            output_tokens=total_output,
            input_tokens=total_input,
            first_seen=first_seen,
            last_seen=last_seen,
        ))

    return sorted(projects, key=lambda p: p.output_tokens, reverse=True)


@dataclass
class HourlyUsage:
    hour: int
    message_count: int
    session_count: int
    tokens: int


def parse_today_hourly() -> list[HourlyUsage]:
    """Parse today's usage broken down by hour (0-23)."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    # hour -> {messages, sessions, tokens}
    hourly: dict[int, dict] = {h: {"messages": 0, "sessions": set(), "tokens": 0} for h in range(24)}

    if not PROJECTS_DIR.exists():
        return []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            if datetime.fromtimestamp(session_file.stat().st_mtime).strftime("%Y-%m-%d") != today_str:
                continue
            try:
                with open(session_file) as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        local_dt = _utc_to_local(d.get("timestamp", ""))
                        if local_dt is None or local_dt.strftime("%Y-%m-%d") != today_str:
                            continue
                        hour = local_dt.hour
                        msg_type = d.get("type")
                        if msg_type == "user":
                            hourly[hour]["messages"] += 1
                            hourly[hour]["sessions"].add(str(session_file))
                        elif msg_type == "assistant":
                            hourly[hour]["messages"] += 1
                            usage = d.get("message", {}).get("usage", {})
                            hourly[hour]["tokens"] += usage.get("output_tokens", 0)
                            hourly[hour]["sessions"].add(str(session_file))
            except (OSError, IOError):
                continue

    return [
        HourlyUsage(
            hour=h,
            message_count=info["messages"],
            session_count=len(info["sessions"]),
            tokens=info["tokens"],
        )
        for h, info in sorted(hourly.items())
    ]


def parse_today_usage() -> Optional[DailyActivity]:
    """Calculate today's usage by scanning recent session files."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    message_count = 0
    session_count = 0
    tool_call_count = 0
    today_tokens: dict = {}

    if not PROJECTS_DIR.exists():
        return None

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            # 오늘 수정된 파일만 검사
            if datetime.fromtimestamp(session_file.stat().st_mtime).strftime("%Y-%m-%d") != today_str:
                continue
            session_has_today = False
            try:
                with open(session_file) as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        local_dt = _utc_to_local(d.get("timestamp", ""))
                        if local_dt is None or local_dt.strftime("%Y-%m-%d") != today_str:
                            continue
                        msg_type = d.get("type")
                        if msg_type == "user":
                            message_count += 1
                            session_has_today = True
                        elif msg_type == "assistant":
                            message_count += 1
                            usage = d.get("message", {}).get("usage", {})
                            out = usage.get("output_tokens", 0)
                            model = d.get("message", {}).get("model", "unknown")
                            today_tokens[model] = today_tokens.get(model, 0) + out
                            # tool_use 블록 카운트
                            content = d.get("message", {}).get("content", [])
                            if isinstance(content, list):
                                tool_call_count += sum(
                                    1 for c in content
                                    if isinstance(c, dict) and c.get("type") == "tool_use"
                                )
            except (OSError, IOError):
                continue
            if session_has_today:
                session_count += 1

    if message_count == 0:
        return None

    return DailyActivity(
        date=today_str,
        message_count=message_count,
        session_count=session_count,
        tool_call_count=tool_call_count,
    ), DailyModelTokens(
        date=today_str,
        tokens_by_model=today_tokens,
    )


def load_usage_data() -> UsageData:
    """Load all usage data from local Claude Code files."""
    hostname = get_hostname()
    stats = parse_stats_cache()

    if stats is None:
        return UsageData(hostname=hostname)

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
