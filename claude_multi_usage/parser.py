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
    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class UsageData:
    hostname: str
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


def parse_projects() -> list[ProjectSummary]:
    """Parse project directories to get project summaries."""
    if not PROJECTS_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        raw_name = project_dir.name
        # 경로 형식: -Users-gimdonghun-workspace-project-subdir
        # "workspace" 이후 부분을 추출하고 중복 제거
        parts = raw_name.split("-")
        try:
            ws_idx = parts.index("workspace")
            path_parts = "-".join(parts[ws_idx + 1:]) if ws_idx + 1 < len(parts) else raw_name
            # 경로 구분자로 분리 후 중복 제거 (e.g., "apr-backend-assignment-apr-backend-assignment")
            # 실제 디렉토리 구조 기반으로 의미 있는 이름 추출
            segments = path_parts.split("-")
            # 연속된 동일 패턴 제거
            project_name = _deduplicate_project_name(path_parts)
        except ValueError:
            project_name = raw_name

        if not project_name or project_name.startswith("-Users"):
            project_name = raw_name.rsplit("-", 1)[-1] or "home"

        # "url-jarvis/-docs" → "url-jarvis-docs"
        project_name = project_name.replace("/-", "-")

        if not project_name:
            continue

        # 세션 파일(.jsonl) 개수 = 세션 수
        session_files = list(project_dir.glob("*.jsonl"))
        session_count = len(session_files)

        # 첫/마지막 세션 시간 (파일 수정시간 기준)
        first_seen = None
        last_seen = None
        if session_files:
            mtimes = [f.stat().st_mtime for f in session_files]
            first_seen = datetime.fromtimestamp(min(mtimes))
            last_seen = datetime.fromtimestamp(max(mtimes))

        projects.append(ProjectSummary(
            name=project_name,
            session_count=session_count,
            first_seen=first_seen,
            last_seen=last_seen,
        ))

    return sorted(projects, key=lambda p: p.session_count, reverse=True)


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
