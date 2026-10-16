"""Install sburn and its periodic sync on another machine over SSH.

``sburn remote install user@host`` runs, on the remote machine:

1. `uv <https://docs.astral.sh/uv/>`_, installed into ``~/.local/bin`` with
   the official installer if the host has none (shell profiles are left
   alone, so ``~/.local/bin`` must be on the PATH there)
2. ``uv tool install`` of sburn, either from this checkout (its git-tracked
   files, streamed over as a tarball and kept in ``~/.local/share/sandburn/src``)
   or from a requirement such as a ``git+https://...`` URL; uv downloads a
   Python if the host has none that is 3.9 or newer
3. ``~/.local/bin/sburn`` and ``~/.local/bin/sandburn`` commands (if
   something else is already there, the commands go to
   ``~/.local/share/sandburn/bin`` instead); an install from before the
   tool was renamed (claude-multi-usage, command ``cmu``) is removed
4. ``sburn config server/key/alias`` with this machine's settings unless
   overridden
5. ``sburn autosync install``, which also performs the first sync

Everything goes through the user's own ``ssh`` and needs ``bash`` and
``curl`` or ``wget`` on the remote; nothing but the source code, the server
URL and the sync keys leaves this machine.
"""

from __future__ import annotations

import io
import shlex
import shutil
import subprocess
import tarfile
from importlib import metadata
from pathlib import Path
from typing import Optional

from .autosync import LAUNCHD_LABEL, LEGACY_LAUNCHD_LABEL, LEGACY_SYSTEMD_UNIT, SYSTEMD_UNIT

PACKAGE_DIR = Path(__file__).resolve().parent
REMOTE_BASE = "~/.local/share/sandburn"
UV_INSTALL_URL = "https://astral.sh/uv/install.sh"
TOOL_NAME = "sandburn"
# Before the rename the tool was claude-multi-usage (command cmu) and lived here.
LEGACY_BASE = "~/.local/share/cmu"
LEGACY_TOOL_NAME = "claude-multi-usage"
# Where uv may live outside PATH on a remote: its installer, Homebrew, /usr/local.
UV_FALLBACKS = ("$HOME/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv")
# Exit status of the uninstall script when the remote had no sburn install.
NOTHING_TO_REMOVE = 3
_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
}
_EXCLUDE_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".egg-info",
    ".pyc",
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
    ".log",
)
_EXCLUDE_PREFIXES = (".env",)


def default_source() -> str:
    """Where the remote should install sburn from.

    A source checkout (pyproject.toml next to the package) is sent as a
    tarball so the remote gets exactly this code. Otherwise the repository
    URL from the package metadata is used as the requirement for uv.
    """
    checkout = PACKAGE_DIR.parent
    if (checkout / "pyproject.toml").exists():
        return str(checkout)
    try:
        md = metadata.metadata("sandburn")
        for entry in md.get_all("Project-URL") or []:
            label, _, url = entry.partition(",")
            if label.strip().lower() == "repository":
                return "git+" + url.strip()
    except metadata.PackageNotFoundError:
        pass
    raise RuntimeError("cannot determine where to install sburn from; pass --from PATH-or-URL")


def _wanted(rel: Path) -> bool:
    parts = rel.parts
    if any(p in _EXCLUDE_DIRS or p.endswith(".egg-info") for p in parts):
        return False
    name = rel.name
    return not (name.endswith(_EXCLUDE_SUFFIXES) or name.startswith(_EXCLUDE_PREFIXES))


def checkout_files(checkout: Path) -> list[Path]:
    """Files to send: git-tracked ones when this is a git checkout, else a
    walk of the tree. Secrets-shaped and build files are never included."""
    files: list[Path] = []
    if (checkout / ".git").exists() and shutil.which("git"):
        res = subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "-z", "--cached"],
            capture_output=True,
            check=False,
        )
        if res.returncode == 0:
            for raw in res.stdout.split(b"\0"):
                if raw:
                    rel = Path(raw.decode("utf-8", errors="surrogateescape"))
                    if (checkout / rel).is_file() and _wanted(rel):
                        files.append(rel)
            return sorted(files)
    for p in sorted(checkout.rglob("*")):
        rel = p.relative_to(checkout)
        if p.is_file() and _wanted(rel):
            files.append(rel)
    return files


def make_tarball(checkout: Path) -> bytes:
    """gzip tarball (top-level directory ``src``) of a source checkout."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in checkout_files(checkout):
            tar.add(checkout / rel, arcname=str(Path("src") / rel), recursive=False)
    return buf.getvalue()


def _find_uv() -> str:
    """Shell lines that set ``UV`` to a usable uv binary or leave it empty.

    A non-interactive ssh shell has a minimal PATH, so the usual install
    locations are tried explicitly (uv's installer, Homebrew, /usr/local).
    """
    candidates = " ".join(f'"{c}"' for c in UV_FALLBACKS)
    return f"""UV=""
for candidate in "$(command -v uv 2>/dev/null || true)" \\
        {candidates}; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then UV="$candidate"; break; fi
done"""


def install_script(
    source_is_tarball: bool,
    requirement: Optional[str],
    server_url: str,
    keys: list[tuple[str, str]],
    alias: Optional[str],
    every_minutes: int,
    python: Optional[str] = None,
    linger: bool = False,
) -> str:
    """The bash script executed on the remote host."""
    q = shlex.quote
    install_from = '"$BASE/src"' if source_is_tarball else q(requirement or "")
    python_flag = f" --python {q(python)}" if python else ""
    key_lines = "\n".join(f'"$SBURN" config key add {q(k)} {q(desc)}' for k, desc in keys)
    alias_line = f'"$SBURN" config alias {q(alias)}' if alias else ""
    linger_flag = " --linger" if linger else ""
    return f"""set -euo pipefail
BASE={REMOTE_BASE}
LEGACY_BASE={LEGACY_BASE}
BIN="$HOME/.local/bin"
mkdir -p "$BASE" "$BIN"
{_find_uv()}
if [ -z "$UV" ]; then
    echo "==> $(hostname): installing uv into $BIN from {UV_INSTALL_URL}"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf {UV_INSTALL_URL} | env UV_INSTALL_DIR="$BIN" UV_NO_MODIFY_PATH=1 sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- {UV_INSTALL_URL} | env UV_INSTALL_DIR="$BIN" UV_NO_MODIFY_PATH=1 sh
    else
        echo "error: curl or wget is needed on $(hostname) to download uv" >&2; exit 1
    fi
    UV="$BIN/uv"
    [ -x "$UV" ] || {{ echo "error: uv installation failed: $UV not found" >&2; exit 1; }}
fi
TOOL_BIN="$("$UV" tool dir)/{TOOL_NAME}/bin"
TOOLS_BIN="$("$UV" tool dir --bin)"
# Never clobber commands that are not ours: put ours next to the tool instead.
for exe in sburn {TOOL_NAME}; do
    if [ -e "$TOOLS_BIN/$exe" ] \\
            && [ "$(readlink "$TOOLS_BIN/$exe" 2>/dev/null || true)" != "$TOOL_BIN/$exe" ]; then
        export UV_TOOL_BIN_DIR="$BASE/bin"
        echo "note: $TOOLS_BIN/$exe exists and is not ours; the sburn command goes to $BASE/bin"
        break
    fi
done
echo "==> $(hostname): installing sburn with uv $("$UV" --version | cut -d' ' -f2)"
"$UV" tool install --quiet --reinstall{python_flag} {install_from}
SBURN="$TOOL_BIN/sburn"
[ -x "$SBURN" ] || {{ echo "error: $SBURN not found after install" >&2; exit 1; }}
echo "sburn is linked in $("$UV" tool dir --bin) (make sure that is on your PATH)"
# An install from before the rename ({LEGACY_TOOL_NAME}, command cmu): the uv tool,
# or the venv-based installer before it. `autosync install` below replaces its job.
if "$UV" tool uninstall {LEGACY_TOOL_NAME} >/dev/null 2>&1; then
    echo "==> $(hostname): removed the pre-rename {LEGACY_TOOL_NAME} tool"
fi
if [ "$(readlink "$BIN/cmu" 2>/dev/null || true)" = "$LEGACY_BASE/venv/bin/cmu" ]; then
    rm -f "$BIN/cmu"
fi
rm -rf "$LEGACY_BASE"
echo "==> $(hostname): configuring"
"$SBURN" config server {q(server_url)}
{key_lines}
{alias_line}
echo "==> $(hostname): scheduling autosync every {every_minutes} min (includes the first sync)"
"$SBURN" autosync install --every {every_minutes}{linger_flag}
"$SBURN" autosync status
"""


def uninstall_script() -> str:
    return f"""set -uo pipefail
BASE={REMOTE_BASE}
LEGACY_BASE={LEGACY_BASE}
BIN="$HOME/.local/bin"
{_find_uv()}
removed=""
job_left=""
if [ -n "$UV" ]; then
    SBURN="$("$UV" tool dir)/{TOOL_NAME}/bin/sburn"
    if [ -x "$SBURN" ]; then
        "$SBURN" autosync uninstall || job_left=1
        "$UV" tool uninstall {TOOL_NAME} >/dev/null 2>&1 || true
        removed="{TOOL_NAME}"
    fi
    # From before the rename ({LEGACY_TOOL_NAME}, command cmu). `sburn autosync
    # uninstall` above also removed its job; without sburn the old command must.
    CMU="$("$UV" tool dir)/{LEGACY_TOOL_NAME}/bin/cmu"
    if [ -x "$CMU" ]; then
        [ -n "$removed" ] || "$CMU" autosync uninstall || job_left=1
        "$UV" tool uninstall {LEGACY_TOOL_NAME} >/dev/null 2>&1 || true
        removed="${{removed:+$removed, }}{LEGACY_TOOL_NAME} (uv tool)"
    fi
fi
# ...or made by the venv-based installer before that.
if [ -x "$LEGACY_BASE/venv/bin/cmu" ]; then
    [ -n "$removed" ] || "$LEGACY_BASE/venv/bin/cmu" autosync uninstall || job_left=1
    removed="${{removed:+$removed, }}{LEGACY_TOOL_NAME} (venv)"
fi
if [ "$(readlink "$BIN/cmu" 2>/dev/null || true)" = "$LEGACY_BASE/venv/bin/cmu" ]; then
    rm -f "$BIN/cmu"
fi
rm -rf "$BASE" "$LEGACY_BASE"
if [ -z "$removed" ]; then
    echo "warning: no sburn install found on $(hostname); nothing to remove" >&2
    exit {NOTHING_TO_REMOVE}
fi
if [ -n "$job_left" ]; then
    echo "==> $(hostname): removed $removed; its autosync job could not be removed" >&2
    echo "    Linux: systemctl --user disable --now {SYSTEMD_UNIT}.timer;" >&2
    echo "           rm ~/.config/systemd/user/{SYSTEMD_UNIT}.*" >&2
    echo "    macOS: launchctl bootout gui/$(id -u)/{LAUNCHD_LABEL};" >&2
    echo "           rm ~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist" >&2
    echo "    (before the rename: {LEGACY_SYSTEMD_UNIT} and {LEGACY_LAUNCHD_LABEL})" >&2
else
    echo "==> $(hostname): removed $removed and its autosync job"
fi
kept=""
for d in ~/.sandburn ~/.claude-multi-usage; do
    if [ -d "$d" ]; then kept="${{kept:+$kept and }}$d"; fi
done
echo "    kept: ${{kept:+the config in $kept, and }}uv"
"""


def _ssh(host: str, args: list[str], input_bytes: bytes, ssh_options: list[str]) -> None:
    cmd = ["ssh", *ssh_options, host, *args]
    subprocess.run(cmd, input=input_bytes, check=True)


def install(
    host: str,
    server_url: str,
    keys: list[tuple[str, str]],
    every_minutes: int,
    alias: Optional[str] = None,
    source: Optional[str] = None,
    python: Optional[str] = None,
    ssh_options: Optional[list[str]] = None,
    linger: bool = False,
    run=_ssh,
) -> list[str]:
    """Install sburn on ``host``. Returns lines describing what was done."""
    ssh_options = ssh_options or []
    source = source or default_source()
    src_path = Path(source).expanduser()
    lines = []
    if src_path.is_dir() and (src_path / "pyproject.toml").exists():
        tarball = make_tarball(src_path)
        unpack = (
            f"rm -rf {REMOTE_BASE}/src && mkdir -p {REMOTE_BASE} && tar -xzf - -C {REMOTE_BASE}"
        )
        run(host, ["bash", "-c", shlex.quote(unpack)], tarball, ssh_options)
        script = install_script(True, None, server_url, keys, alias, every_minutes, python, linger)
        lines.append(f"sent this checkout ({len(tarball) // 1024} KB) to {host}:{REMOTE_BASE}/src")
    else:
        script = install_script(
            False, source, server_url, keys, alias, every_minutes, python, linger
        )
        lines.append(f"{host} installs from {source}")
    run(host, ["bash", "-s"], script.encode(), ssh_options)
    lines.append(f"sburn installed on {host} with autosync every {every_minutes} min")
    return lines


def uninstall(host: str, ssh_options: Optional[list[str]] = None, run=_ssh) -> list[str]:
    try:
        run(host, ["bash", "-s"], uninstall_script().encode(), ssh_options or [])
    except subprocess.CalledProcessError as e:
        if e.returncode == NOTHING_TO_REMOVE:
            return [f"no sburn install found on {host}; nothing removed"]
        raise
    return [f"sburn removed from {host}"]
