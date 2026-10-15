"""Pydantic models for the sync server API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

# NOTE: ``Optional[str]`` rather than ``str | None`` - Pydantic evaluates these
# annotations at import time, and the ``|`` form fails on Python 3.9, which
# pyproject.toml declares as supported.


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
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class DeviceHourlyUsage(BaseModel):
    hour: int
    message_count: int
    session_count: int
    tokens: int


class SyncPayload(BaseModel):
    """Data pushed from a device to the server."""
    hostname: str
    synced_at: str
    alias: Optional[str] = None
    keys: list[str] = []
    # backward compat: accept email and convert to keys
    email: Optional[str] = None
    daily_activity: list[DeviceActivity] = []
    daily_model_tokens: list[DeviceModelTokens] = []
    model_usage: list[DeviceModelUsage] = []
    projects: list[DeviceProject] = []
    hour_counts: dict[str, int] = {}
    today_hourly: list[DeviceHourlyUsage] = []
    # The local date (on the device) that today_hourly describes
    today_hourly_date: Optional[str] = None
    # True when the payload is a complete picture of the device (e.g. a
    # claude.ai export): the server then replaces stored data instead of
    # merging day by day.
    snapshot: bool = False
    total_sessions: int = 0
    total_messages: int = 0
    first_session_date: Optional[str] = None

    def model_post_init(self, __context) -> None:
        # Migrate legacy "email" field to keys
        if self.email and not self.keys:
            self.keys = [self.email]
        self.email = None


class DeviceInfo(BaseModel):
    hostname: str
    alias: Optional[str] = None
    keys: list[str] = []
    last_synced: str
    total_sessions: int
    total_messages: int
