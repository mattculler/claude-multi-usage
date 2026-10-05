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


def _is_duplicate_message(message: dict, seen_msg_ids: set) -> bool:
    """Return True if this assistant message was already counted.

    Claude Code writes one JSONL line per content block of an assistant
    message (text, tool_use, ...), and every line repeats the same
    ``message.id`` and ``usage``.  Token counts and costs must therefore be
    accumulated only once per message id.  Messages without an id (e.g.
    synthetic ones) are never treated as duplicates.
    """
    msg_id = message.get("id") if isinstance(message, dict) else None
    if not msg_id:
        return False
    if msg_id in seen_msg_ids:
        return True
    seen_msg_ids.add(msg_id)
    return False


def _parse_session_tokens(session_file: Path, calc_cost: bool = False,
                          seen_msg_ids: set | None = None) -> tuple:
    """Parse a session jsonl file and return (output_tokens, input_tokens, timestamps, cost).

    ``seen_msg_ids`` may be shared across files so that resumed or forked
    sessions, which copy earlier history into a new file, are not counted twice.
    """
    from .pricing import calculate_model_cost

    if seen_msg_ids is None:
        seen_msg_ids = set()
    output_tokens = 0
    input_tokens = 0
    timestamps = []
    cost = 0.0
    try:
        with open(session_file) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "assistant":
                    message = d.get("message", {})
                    if not _is_duplicate_message(message, seen_msg_ids):
                        usage = message.get("usage", {})
                        out = usage.get("output_tokens", 0)
                        inp = usage.get("input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        output_tokens += out
                        input_tokens += inp + cache_read + cache_create
                        if calc_cost:
                            model = message.get("model", "")
                            cost += calculate_model_cost(model, inp, out, cache_read, cache_create)
                ts = d.get("timestamp")
                if ts:
                    timestamps.append(ts)
    except (OSError, IOError):
        pass
    return output_tokens, input_tokens, timestamps, cost


def parse_projects(with_tokens: bool = True) -> list:
    """Parse project directories to get project summaries."""
    from .pricing import get_pricing

    if not PROJECTS_DIR.exists():
        return []

    has_pricing = get_pricing() is not None
    seen_msg_ids: set = set()
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
        total_cost = 0.0
        all_timestamps = []

        if with_tokens:
            for sf in session_files:
                out_t, in_t, ts_list, sf_cost = _parse_session_tokens(
                    sf, calc_cost=has_pricing, seen_msg_ids=seen_msg_ids)
                total_output += out_t
                total_input += in_t
                total_cost += sf_cost
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
            cost=total_cost,
        ))

    return sorted(projects, key=lambda p: p.output_tokens, reverse=True)


def parse_today_hourly() -> list[HourlyUsage]:
    """Parse today's usage broken down by hour (0-23)."""
    from .pricing import calculate_model_cost, get_pricing

    today_str = datetime.now().strftime("%Y-%m-%d")
    has_pricing = get_pricing() is not None
    # hour -> {messages, sessions, tokens, cost}
    hourly: dict[int, dict] = {h: {"messages": 0, "sessions": set(), "tokens": 0, "cost": 0.0} for h in range(24)}

    if not PROJECTS_DIR.exists():
        return []

    seen_msg_ids: set = set()
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
                            hourly[hour]["sessions"].add(str(session_file))
                            message = d.get("message", {})
                            if _is_duplicate_message(message, seen_msg_ids):
                                continue
                            usage = message.get("usage", {})
                            out = usage.get("output_tokens", 0)
                            hourly[hour]["tokens"] += out
                            if has_pricing:
                                inp = usage.get("input_tokens", 0)
                                cache_read = usage.get("cache_read_input_tokens", 0)
                                cache_create = usage.get("cache_creation_input_tokens", 0)
                                model = message.get("model", "")
                                hourly[hour]["cost"] += calculate_model_cost(
                                    model, inp, out, cache_read, cache_create
                                )
            except (OSError, IOError):
                continue

    return [
        HourlyUsage(
            hour=h,
            message_count=info["messages"],
            session_count=len(info["sessions"]),
            tokens=info["tokens"],
            cost=info["cost"],
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

    seen_msg_ids: set = set()
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            # Only inspect files modified today
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
                            message = d.get("message", {})
                            # Count tool_use blocks (each JSONL line carries its own block)
                            content = message.get("content", [])
                            if isinstance(content, list):
                                tool_call_count += sum(
                                    1 for c in content
                                    if isinstance(c, dict) and c.get("type") == "tool_use"
                                )
                            if _is_duplicate_message(message, seen_msg_ids):
                                continue
                            usage = message.get("usage", {})
                            out = usage.get("output_tokens", 0)
                            model = message.get("model", "unknown")
                            today_tokens[model] = today_tokens.get(model, 0) + out
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
