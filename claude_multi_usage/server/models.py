"""Pydantic models for the sync server API."""

from __future__ import annotations

from pydantic import BaseModel


class DeviceActivity(BaseModel):
    date: str
    message_count: int
    session_count: int
    tool_call_count: int


class DeviceModelTokens(BaseModel):
    date: str
    tokens_by_model: dict[str, int]


class DeviceModelUsage(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


class DeviceProject(BaseModel):
    name: str
    session_count: int
    output_tokens: int = 0
    input_tokens: int = 0
    first_seen: str | None = None
    last_seen: str | None = None


class DeviceHourlyUsage(BaseModel):
    hour: int
    message_count: int
    session_count: int
    tokens: int


class SyncPayload(BaseModel):
    """Data pushed from a device to the server."""
    hostname: str
    synced_at: str
    alias: str | None = None
    keys: list[str] = []
    # backward compat: accept email and convert to keys
    email: str | None = None
    daily_activity: list[DeviceActivity] = []
    daily_model_tokens: list[DeviceModelTokens] = []
    model_usage: list[DeviceModelUsage] = []
    projects: list[DeviceProject] = []
    hour_counts: dict[str, int] = {}
    today_hourly: list[DeviceHourlyUsage] = []
    total_sessions: int = 0
    total_messages: int = 0
    first_session_date: str | None = None

    def model_post_init(self, __context) -> None:
        # email → keys 마이그레이션
        if self.email and not self.keys:
            self.keys = [self.email]
        self.email = None


class DeviceInfo(BaseModel):
    hostname: str
    alias: str | None = None
    keys: list[str] = []
    last_synced: str
    total_sessions: int
    total_messages: int
