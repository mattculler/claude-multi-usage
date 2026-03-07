"""Configuration management for claude-multi-usage."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".claude-multi-usage"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "server_url": None,
}


def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        # 기본값 병합
        merged = dict(DEFAULT_CONFIG)
        merged.update(config)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    _ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_server_url() -> str | None:
    return load_config().get("server_url")


def set_server_url(url: str) -> None:
    config = load_config()
    config["server_url"] = url.rstrip("/")
    save_config(config)
