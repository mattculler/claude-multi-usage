"""Configuration management for claude-multi-usage."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".claude-multi-usage"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "server_url": None,
    "keys": [],
    "alias": None,
}


def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(config)
        # Migrate the legacy "email" field to keys
        if "email" in merged and merged["email"] and not merged.get("keys"):
            merged["keys"] = [{"key": merged["email"], "description": "migrated from email"}]
        merged.pop("email", None)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    _ensure_config_dir()
    config.pop("email", None)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_server_url() -> str | None:
    return load_config().get("server_url")


def set_server_url(url: str) -> None:
    config = load_config()
    config["server_url"] = url.rstrip("/")
    save_config(config)


def get_keys() -> list[dict]:
    """Return list of {key, description} dicts."""
    return load_config().get("keys", [])


def add_key(key: str, description: str = "") -> bool:
    """Add or update a key. Returns True if newly added, False if updated."""
    config = load_config()
    keys = config.get("keys", [])
    for entry in keys:
        if entry["key"] == key:
            entry["description"] = description
            config["keys"] = keys
            save_config(config)
            return False
    keys.append({"key": key, "description": description})
    config["keys"] = keys
    save_config(config)
    return True


def remove_key(key: str) -> bool:
    """Remove a key. Returns True if found and removed."""
    config = load_config()
    keys = config.get("keys", [])
    new_keys = [k for k in keys if k["key"] != key]
    if len(new_keys) == len(keys):
        return False
    config["keys"] = new_keys
    save_config(config)
    return True


# Backward compatibility
def get_email() -> str | None:
    """Deprecated: returns first key if any, for backward compat."""
    keys = get_keys()
    return keys[0]["key"] if keys else None


def set_email(email: str) -> None:
    """Deprecated: adds email as a key for backward compat."""
    add_key(email.strip().lower(), "migrated from email")


def get_alias() -> str | None:
    return load_config().get("alias")


def set_alias(alias: str | None) -> None:
    config = load_config()
    config["alias"] = alias if alias else None
    save_config(config)


