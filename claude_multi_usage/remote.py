"""Install cmu and its periodic sync on another machine over SSH.

``cmu remote install user@host`` runs, on the remote machine:

1. a check for Python 3.9+ with venv and ensurepip
2. a virtualenv in ``~/.local/share/cmu/venv`` with cmu installed into it,
   either from this checkout (its git-tracked files, streamed over as a
   tarball) or from a pip requirement such as a ``git+https://...`` URL
3. a ``~/.local/bin/cmu`` symlink (left alone if something else is there)
4. ``cmu config server/key/alias`` with this machine's settings unless
   overridden
5. ``cmu autosync install``, which also performs the first sync

Everything goes through the user's own ``ssh`` and needs ``bash`` on the
remote; nothing but the source code, the server URL and the sync keys
leaves this machine.
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

PACKAGE_DIR = Path(__file__).resolve().parent
REMOTE_BASE = "~/.local/share/cmu"
_EXCLUDE_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache",
                 ".ruff_cache", ".claude"}
_EXCLUDE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".egg-info", ".pyc",
                     ".zip", ".tar", ".tgz", ".gz", ".log")
_EXCLUDE_PREFIXES = (".env",)


def default_source() -> str:
    """Where the remote should install cmu from.

    A source checkout (pyproject.toml next to the package) is sent as a
    tarball so the remote gets exactly this code. Otherwise the repository
    URL from the package metadata is used as a pip requirement.
    """
    checkout = PACKAGE_DIR.parent
    if (checkout / "pyproject.toml").exists():
        return str(checkout)
    try:
        md = metadata.metadata("claude-multi-usage")
        for entry in md.get_all("Project-URL") or []:
            label, _, url = entry.partition(",")
            if label.strip().lower() == "repository":
                return "git+" + url.strip()
    except metadata.PackageNotFoundError:
        pass
    raise RuntimeError("cannot determine where to install cmu from; pass --from PATH-or-URL")


def _wanted(rel: Path) -> bool:
    parts = rel.parts
    if any(p in _EXCLUDE_DIRS or p.endswith(".egg-info") for p in parts):
        return False
    name = rel.name
    if name.endswith(_EXCLUDE_SUFFIXES) or name.startswith(_EXCLUDE_PREFIXES):
        return False
    return True


def checkout_files(checkout: Path) -> list[Path]:
    """Files to send: git-tracked ones when this is a git checkout, else a
    walk of the tree. Secrets-shaped and build files are never included."""
    files: list[Path] = []
    if (checkout / ".git").exists() and shutil.which("git"):
        res = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z", "--cached"],
                             capture_output=True, check=False)
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


def install_script(source_is_tarball: bool, requirement: Optional[str], server_url: str,
                   keys: list[tuple[str, str]], alias: Optional[str], every_minutes: int,
                   python: str = "python3", linger: bool = False) -> str:
    """The bash script executed on the remote host."""
    q = shlex.quote
    install_from = '"$BASE/src"' if source_is_tarball else q(requirement or "")
    key_lines = "\n".join(
        f'"$VENV/bin/cmu" config key add {q(k)} {q(desc)}' for k, desc in keys
    )
    alias_line = f'"$VENV/bin/cmu" config alias {q(alias)}' if alias else ""
    linger_flag = " --linger" if linger else ""
    return f"""set -euo pipefail
PY={q(python)}
command -v "$PY" >/dev/null 2>&1 || {{ echo "error: $PY not found on $(hostname)" >&2; exit 1; }}
"$PY" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \\
    || {{ echo "error: $PY must be Python 3.9+ with venv and ensurepip (Debian/Ubuntu: apt install python3-venv; macOS: xcode-select --install)" >&2; exit 1; }}
BASE={REMOTE_BASE}
VENV="$BASE/venv"
LINK="$HOME/.local/bin/cmu"
mkdir -p "$BASE" "$HOME/.local/bin"
echo "==> $(hostname): virtualenv in $VENV"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    rm -rf "$VENV"
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
echo "==> $(hostname): installing cmu"
"$VENV/bin/python" -m pip install --quiet --upgrade {install_from}
rm -rf "$BASE/src"
if [ ! -e "$LINK" ] || [ "$(readlink "$LINK" 2>/dev/null || true)" = "$VENV/bin/cmu" ]; then
    ln -sfn "$VENV/bin/cmu" "$LINK"
else
    echo "note: $LINK already exists and is not ours; left untouched (cmu is at $VENV/bin/cmu)"
fi
echo "cmu is linked at $LINK (make sure $HOME/.local/bin is on your PATH in interactive shells)"
echo "==> $(hostname): configuring"
"$VENV/bin/cmu" config server {q(server_url)}
{key_lines}
{alias_line}
echo "==> $(hostname): scheduling autosync every {every_minutes} min (includes the first sync)"
"$VENV/bin/cmu" autosync install --every {every_minutes}{linger_flag}
"$VENV/bin/cmu" autosync status
"""


def uninstall_script() -> str:
    return f"""set -uo pipefail
BASE={REMOTE_BASE}
LINK="$HOME/.local/bin/cmu"
if [ -x "$BASE/venv/bin/cmu" ]; then "$BASE/venv/bin/cmu" autosync uninstall || true; fi
if [ "$(readlink "$LINK" 2>/dev/null || true)" = "$BASE/venv/bin/cmu" ]; then rm -f "$LINK"; fi
rm -rf "$BASE"
echo "==> $(hostname): removed cmu and its autosync job (config in ~/.claude-multi-usage kept)"
"""


def _ssh(host: str, args: list[str], input_bytes: bytes, ssh_options: list[str]) -> None:
    cmd = ["ssh", *ssh_options, host, *args]
    subprocess.run(cmd, input=input_bytes, check=True)


def install(host: str, server_url: str, keys: list[tuple[str, str]], every_minutes: int,
            alias: Optional[str] = None, source: Optional[str] = None, python: str = "python3",
            ssh_options: Optional[list[str]] = None, linger: bool = False, run=_ssh) -> list[str]:
    """Install cmu on ``host``. Returns lines describing what was done."""
    ssh_options = ssh_options or []
    source = source or default_source()
    src_path = Path(source).expanduser()
    lines = []
    if src_path.is_dir() and (src_path / "pyproject.toml").exists():
        tarball = make_tarball(src_path)
        unpack = f"rm -rf {REMOTE_BASE}/src && mkdir -p {REMOTE_BASE} && tar -xzf - -C {REMOTE_BASE}"
        run(host, ["bash", "-c", shlex.quote(unpack)], tarball, ssh_options)
        script = install_script(True, None, server_url, keys, alias, every_minutes, python, linger)
        lines.append(f"sent this checkout ({len(tarball) // 1024} KB) to {host}:{REMOTE_BASE}/src")
    else:
        script = install_script(False, source, server_url, keys, alias, every_minutes, python, linger)
        lines.append(f"{host} installs from {source}")
    run(host, ["bash", "-s"], script.encode(), ssh_options)
    lines.append(f"cmu installed on {host} with autosync every {every_minutes} min")
    return lines


def uninstall(host: str, ssh_options: Optional[list[str]] = None, run=_ssh) -> list[str]:
    run(host, ["bash", "-s"], uninstall_script().encode(), ssh_options or [])
    return [f"cmu removed from {host}"]
