"""Periodic background sync, installed per user.

``cmu autosync install`` registers a scheduler job (a systemd user timer on
Linux, a launchd agent on macOS) that runs ``cmu autosync run`` every few
minutes. ``run`` fingerprints the local usage files and only talks to the
server when the fingerprint differs from the one recorded at the last
successful sync, so an idle machine stays silent.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import parser as _parser

CACHE_DIR = Path.home() / ".claude-multi-usage"
STATE_FILE = CACHE_DIR / "autosync-state.json"

SYSTEMD_UNIT = "cmu-autosync"
LAUNCHD_LABEL = "com.claude-multi-usage.autosync"
DEFAULT_EVERY_MINUTES = 15


def systemd_user_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


# -- change detection ---------------------------------------------------------

def fingerprint() -> str:
    """Hash of every local usage file's path, size and mtime."""
    h = hashlib.sha256()
    entries = []
    stats = _parser.STATS_FILE
    if stats.exists():
        st = stats.stat()
        entries.append(("stats-cache.json", st.st_size, st.st_mtime_ns))
    projects = _parser.PROJECTS_DIR
    if projects.exists():
        for f in projects.rglob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            entries.append((str(f.relative_to(projects)), st.st_size, st.st_mtime_ns))
    for entry in sorted(entries):
        h.update(repr(entry).encode())
    return h.hexdigest()


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def run(sync: Callable[[], None], force: bool = False) -> str:
    """Sync if the usage files changed since the last successful sync.

    ``sync`` performs the actual upload and raises on failure. Returns
    "unchanged", "synced" or "failed".
    """
    state = load_state()
    current = fingerprint()
    now = datetime.now().isoformat(timespec="seconds")
    state["last_run"] = now
    if not force and state.get("fingerprint") == current:
        state["last_result"] = "unchanged"
        save_state(state)
        return "unchanged"
    try:
        sync()
    except Exception as e:  # recorded, then re-raised for the caller to report
        state["last_result"] = f"failed: {e}"
        save_state(state)
        raise
    state.update({"fingerprint": current, "last_sync": now, "last_result": "synced"})
    save_state(state)
    return "synced"


# -- scheduler backends -------------------------------------------------------

def _command() -> list[str]:
    """How the scheduler should invoke cmu: the interpreter this install uses."""
    return [sys.executable, "-m", "claude_multi_usage.cli", "autosync", "run", "--quiet"]


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def backend() -> str:
    system = platform.system()
    if system == "Linux" and shutil.which("systemctl"):
        return "systemd"
    if system == "Darwin":
        return "launchd"
    return "unsupported"


def install(every_minutes: int = DEFAULT_EVERY_MINUTES, linger: bool = False) -> list[str]:
    """Install and start the scheduler job. Returns lines describing what was done."""
    if every_minutes < 1:
        raise ValueError("--every must be at least 1 minute")
    kind = backend()
    if kind == "systemd":
        return _install_systemd(every_minutes, linger)
    if kind == "launchd":
        return _install_launchd(every_minutes)
    raise RuntimeError(
        "no supported scheduler on this system (need systemd on Linux or launchd on macOS). "
        f"Schedule this yourself instead: {' '.join(_command())}"
    )


def uninstall() -> list[str]:
    kind = backend()
    if kind == "systemd":
        return _uninstall_systemd()
    if kind == "launchd":
        return _uninstall_launchd()
    return ["nothing to remove: no supported scheduler on this system"]


def status() -> dict:
    kind = backend()
    info: dict = {"backend": kind, "installed": False, "state": load_state()}
    if kind == "systemd":
        unit_dir = systemd_user_dir()
        info["installed"] = (unit_dir / f"{SYSTEMD_UNIT}.timer").exists()
        if info["installed"]:
            res = _run(["systemctl", "--user", "is-enabled", f"{SYSTEMD_UNIT}.timer"], check=False)
            info["enabled"] = res.stdout.strip() or res.stderr.strip()
            res = _run(["systemctl", "--user", "list-timers", f"{SYSTEMD_UNIT}.timer", "--no-pager",
                        "--no-legend"], check=False)
            info["timer"] = res.stdout.strip()
    elif kind == "launchd":
        plist = launch_agents_dir() / f"{LAUNCHD_LABEL}.plist"
        info["installed"] = plist.exists()
        if info["installed"]:
            res = _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
            info["enabled"] = "loaded" if res.returncode == 0 else "not loaded"
    return info


def _install_systemd(every_minutes: int, linger: bool) -> list[str]:
    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    exec_start = " ".join(_quote_systemd(part) for part in _command())
    (unit_dir / f"{SYSTEMD_UNIT}.service").write_text(
        "[Unit]\n"
        "Description=claude-multi-usage: sync local usage to the server when it changed\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )
    (unit_dir / f"{SYSTEMD_UNIT}.timer").write_text(
        "[Unit]\n"
        f"Description=Run cmu autosync every {every_minutes} minutes\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        f"OnUnitActiveSec={every_minutes}min\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])
    lines = [
        f"installed {unit_dir / (SYSTEMD_UNIT + '.timer')} (every {every_minutes} min)",
        "the timer runs while you are logged in",
    ]
    if linger:
        res = _run(["loginctl", "enable-linger"], check=False)
        if res.returncode == 0:
            lines[-1] = "lingering enabled: the timer also runs while you are logged out"
        else:
            lines.append(f"could not enable lingering ({(res.stderr or res.stdout).strip()}); "
                         "run: sudo loginctl enable-linger $USER")
    else:
        lines.append("to also run while logged out: loginctl enable-linger (or --linger)")
    return lines


def _quote_systemd(part: str) -> str:
    if any(ch in part for ch in " \t\"'\\"):
        return '"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return part


def _uninstall_systemd() -> list[str]:
    unit_dir = systemd_user_dir()
    _run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"], check=False)
    removed = []
    for name in (f"{SYSTEMD_UNIT}.timer", f"{SYSTEMD_UNIT}.service"):
        p = unit_dir / name
        if p.exists():
            p.unlink()
            removed.append(str(p))
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    return [f"removed {p}" for p in removed] or ["nothing installed"]


def _install_launchd(every_minutes: int) -> list[str]:
    agents = launch_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{LAUNCHD_LABEL}.plist"
    log_dir = CACHE_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as f:
        plistlib.dump({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": _command(),
            "StartInterval": every_minutes * 60,
            "RunAtLoad": True,
            "StandardOutPath": str(log_dir / "autosync.log"),
            "StandardErrorPath": str(log_dir / "autosync.log"),
        }, f)
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", domain, str(plist)], check=False)
    _run(["launchctl", "bootstrap", domain, str(plist)])
    return [f"installed {plist} (every {every_minutes} min)",
            f"log: {log_dir / 'autosync.log'}"]


def _uninstall_launchd() -> list[str]:
    plist = launch_agents_dir() / f"{LAUNCHD_LABEL}.plist"
    _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False)
    if plist.exists():
        plist.unlink()
        return [f"removed {plist}"]
    return ["nothing installed"]


def describe_status(info: dict) -> list[str]:
    lines = [f"backend:   {info['backend']}",
             f"installed: {'yes' if info['installed'] else 'no'}"]
    if info.get("enabled"):
        lines.append(f"enabled:   {info['enabled']}")
    if info.get("timer"):
        lines.append(f"timer:     {info['timer']}")
    state = info.get("state") or {}
    for key, label in (("last_run", "last run"), ("last_sync", "last sync"), ("last_result", "result")):
        if state.get(key):
            lines.append(f"{label + ':':<11}{state[key]}")
    return lines
