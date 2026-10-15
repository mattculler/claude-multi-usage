"""Periodic background sync, installed per user.

``cmu autosync install`` registers a scheduler job (a systemd user timer on
Linux, a launchd agent on macOS) that runs ``cmu autosync run`` every few
minutes. ``run`` fingerprints the local usage files and the sync settings
and only talks to the server when the fingerprint differs from the one
recorded at the last successful sync, so an idle machine stays silent.
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
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import parser as _parser
from .config import get_alias, get_keys, get_server_url

CACHE_DIR = Path.home() / ".claude-multi-usage"
STATE_FILE = CACHE_DIR / "autosync-state.json"
LOCK_FILE = CACHE_DIR / "autosync.lock"

SYSTEMD_UNIT = "cmu-autosync"
LAUNCHD_LABEL = "com.claude-multi-usage.autosync"
DEFAULT_EVERY_MINUTES = 15
SYSTEMD_BOOTED_DIR = Path("/run/systemd/system")


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    if not os.path.isabs(base):
        base = str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


# -- change detection ---------------------------------------------------------

def fingerprint() -> str:
    """Hash of every local usage file's path, size and mtime, plus the sync settings."""
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
    # A new server, key or alias must be pushed even if no session changed.
    config = (get_server_url(), tuple(sorted(k["key"] for k in get_keys())), get_alias())
    h.update(repr(config).encode())
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
    fd, tmp = tempfile.mkstemp(prefix="autosync-state.", suffix=".tmp", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_success(current: str | None = None) -> None:
    """Remember that the current local state has been synced (plain `cmu sync`)."""
    state = load_state()
    now = _now()
    state.update({"fingerprint": current or fingerprint(), "last_run": now,
                  "last_sync": now, "last_result": "synced"})
    save_state(state)


@contextmanager
def _single_flight():
    """Yield True while holding the autosync lock, False if another run holds it."""
    try:
        import fcntl
    except ImportError:  # not POSIX: run without a lock
        yield True
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def run(sync: Callable[[], object], force: bool = False) -> str:
    """Sync if the usage files or settings changed since the last successful sync.

    ``sync`` performs the actual upload and raises on failure. Returns
    "unchanged", "synced" or "busy" (another run is in progress, e.g. the
    timer and a shell wrapper at once); a failure is recorded in the state
    and re-raised.
    """
    with _single_flight() as acquired:
        if not acquired:
            return "busy"
        state = load_state()
        current = fingerprint()
        state["last_run"] = _now()
        if not force and state.get("fingerprint") == current:
            state["last_result"] = "unchanged"
            save_state(state)
            return "unchanged"
        try:
            sync()
        except Exception as e:
            state["last_result"] = f"failed: {e}"
            save_state(state)
            raise
        record_success(current)
        return "synced"


# -- scheduler backends -------------------------------------------------------

def _command() -> list[str]:
    """How the scheduler invokes cmu.

    The console script next to this interpreter is preferred: unlike
    ``python -m`` it does not put the working directory on sys.path.
    """
    script = Path(sys.executable).with_name("cmu")
    if script.exists():
        return [str(script), "autosync", "run", "--quiet"]
    return [sys.executable, "-m", "claude_multi_usage.cli", "autosync", "run", "--quiet"]


def _systemd_env() -> dict:
    """Environment for systemctl --user, filled in for non-login shells (ssh)."""
    env = dict(os.environ)
    uid = os.getuid()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    if "DBUS_SESSION_BUS_ADDRESS" not in env and Path(runtime, "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return env


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    env = _systemd_env() if cmd and cmd[0] in ("systemctl", "loginctl") else None
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)


def backend() -> str:
    system = platform.system()
    if system == "Linux" and shutil.which("systemctl") and SYSTEMD_BOOTED_DIR.is_dir():
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
        "no supported scheduler on this system (need a running systemd on Linux or launchd on macOS). "
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
            loaded = any(
                _run(["launchctl", "print", f"{d}/{os.getuid()}/{LAUNCHD_LABEL}"], check=False).returncode == 0
                for d in ("gui", "user"))
            info["enabled"] = "loaded" if loaded else "not loaded"
    return info


def _install_systemd(every_minutes: int, linger: bool) -> list[str]:
    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    exec_start = " ".join(_quote_systemd(part) for part in _command())
    service = unit_dir / f"{SYSTEMD_UNIT}.service"
    timer = unit_dir / f"{SYSTEMD_UNIT}.timer"
    service.write_text(
        "[Unit]\n"
        "Description=claude-multi-usage: sync local usage to the server when it changed\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={_quote_systemd(str(CACHE_DIR))}\n"
        f"ExecStart={exec_start}\n"
    )
    timer.write_text(
        "[Unit]\n"
        f"Description=Run cmu autosync every {every_minutes} minutes\n"
        "\n"
        "[Timer]\n"
        # Relative to the timer's own activation, so it fires after every
        # login or boot. (OnBootSec + Persistent stops firing for good once
        # the user manager starts more than a few minutes after boot.)
        "OnActiveSec=1min\n"
        f"OnUnitActiveSec={every_minutes}min\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    try:
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])
    except subprocess.CalledProcessError:
        for p in (timer, service):
            p.unlink(missing_ok=True)
        raise
    lines = [
        f"installed {timer} (every {every_minutes} min)",
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
    """Quote one ExecStart argument; % and $ are specifiers even unquoted."""
    part = part.replace("%", "%%").replace("$", "$$")
    if any(ch in part for ch in " \t\"'\\"):
        return '"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return part


def _timer_stamp() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or ""
    if not os.path.isabs(data_home):
        data_home = str(Path.home() / ".local" / "share")
    return Path(data_home) / "systemd" / "timers" / f"stamp-{SYSTEMD_UNIT}.timer"


def _uninstall_systemd() -> list[str]:
    unit_dir = systemd_user_dir()
    _run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"], check=False)
    _run(["systemctl", "--user", "clean", "--what=state", f"{SYSTEMD_UNIT}.timer"], check=False)
    _timer_stamp().unlink(missing_ok=True)
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
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as f:
        plistlib.dump({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": _command(),
            "WorkingDirectory": str(CACHE_DIR),
            "StartInterval": every_minutes * 60,
            "RunAtLoad": True,
            "StandardOutPath": str(CACHE_DIR / "autosync.log"),
            "StandardErrorPath": str(CACHE_DIR / "autosync.log"),
        }, f)
    lines = [f"installed {plist} (every {every_minutes} min)",
             f"log: {CACHE_DIR / 'autosync.log'}"]
    domain = _launchd_domain()
    _run(["launchctl", "bootout", domain, str(plist)], check=False)
    _wait_for_bootout(domain)
    res = _run(["launchctl", "bootstrap", domain, str(plist)], check=False)
    if res.returncode == 0:
        lines.append(f"loaded into {domain}")
    else:
        # No session to load into (ssh without a console login): launchd
        # loads LaunchAgents itself at the next login.
        lines.append(f"not loaded now ({(res.stderr or res.stdout).strip() or 'no login session'}); "
                     "it starts at the next login")
    return lines


def _launchd_domain() -> str:
    """gui/<uid> when the user is logged in at the console, else user/<uid>."""
    uid = os.getuid()
    if _run(["launchctl", "print", f"gui/{uid}"], check=False).returncode == 0:
        return f"gui/{uid}"
    return f"user/{uid}"


def _wait_for_bootout(domain: str, attempts: int = 20) -> None:
    import time

    for _ in range(attempts):
        if _run(["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"], check=False).returncode != 0:
            return
        time.sleep(0.25)


def _uninstall_launchd() -> list[str]:
    plist = launch_agents_dir() / f"{LAUNCHD_LABEL}.plist"
    for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
        _run(["launchctl", "bootout", domain, str(plist)], check=False)
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
