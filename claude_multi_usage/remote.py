"""Install cmu and its periodic sync on another machine over SSH.

``cmu remote install user@host`` runs, on the remote machine:

1. a check for Python 3.9+ with venv and ensurepip
2. a virtualenv in ``~/.local/share/cmu/venv`` with cmu installed into it,
   either from this checkout (streamed over as a tarball) or from a pip
   requirement such as a ``git+https://...`` URL
3. a ``~/.local/bin/cmu`` symlink
4. ``cmu config server/key/alias`` with this machine's settings unless
   overridden
5. ``cmu autosync install`` and a first ``cmu autosync run``

Everything goes through the user's own ``ssh``; no credentials other than
the server URL and sync keys leave this machine.
"""

from __future__ import annotations

import io
import shlex
import subprocess
import tarfile
from importlib import metadata
from pathlib import Path
from typing import Optional

PACKAGE_DIR = Path(__file__).resolve().parent
REMOTE_BASE = "~/.local/share/cmu"
_EXCLUDE_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}


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


def make_tarball(checkout: Path) -> bytes:
    """gzip tarball of a source checkout without build artefacts or git data."""
    buf = io.BytesIO()

    def keep(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        parts = Path(info.name).parts
        if any(p in _EXCLUDE_DIRS or p.endswith(".egg-info") for p in parts):
            return None
        return info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(checkout, arcname="src", filter=keep)
    return buf.getvalue()


def install_script(source_is_tarball: bool, requirement: str | None, server_url: str,
                   keys: list[tuple[str, str]], alias: str | None, every_minutes: int,
                   python: str = "python3") -> str:
    """The bash script executed on the remote host."""
    q = shlex.quote
    base = REMOTE_BASE
    if source_is_tarball:
        install_from = '"$BASE/src"'
    else:
        install_from = q(requirement or "")
    key_lines = "\n".join(
        f'"$VENV/bin/cmu" config key add {q(k)} {q(desc)}' for k, desc in keys
    )
    alias_line = f'"$VENV/bin/cmu" config alias {q(alias)}' if alias else ""
    return f"""set -euo pipefail
PY={q(python)}
command -v "$PY" >/dev/null 2>&1 || {{ echo "error: $PY not found on $(hostname)" >&2; exit 1; }}
"$PY" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \\
    || {{ echo "error: $PY must be Python 3.9+ with venv and ensurepip (Debian/Ubuntu: apt install python3-venv)" >&2; exit 1; }}
BASE={base}
VENV="$BASE/venv"
mkdir -p "$BASE" "$HOME/.local/bin"
echo "==> $(hostname): virtualenv in $VENV"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    rm -rf "$VENV"
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
echo "==> $(hostname): installing cmu"
"$VENV/bin/python" -m pip install --quiet --upgrade {install_from}
ln -sf "$VENV/bin/cmu" "$HOME/.local/bin/cmu"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add $HOME/.local/bin to PATH on $(hostname) to run cmu directly" ;; esac
echo "==> $(hostname): configuring"
"$VENV/bin/cmu" config server {q(server_url)}
{key_lines}
{alias_line}
echo "==> $(hostname): scheduling autosync every {every_minutes} min"
"$VENV/bin/cmu" autosync install --every {every_minutes}
"$VENV/bin/cmu" autosync run --force
"$VENV/bin/cmu" autosync status
"""


def uninstall_script() -> str:
    return f"""set -uo pipefail
BASE={REMOTE_BASE}
if [ -x "$BASE/venv/bin/cmu" ]; then "$BASE/venv/bin/cmu" autosync uninstall || true; fi
rm -rf "$BASE" "$HOME/.local/bin/cmu"
echo "==> $(hostname): removed cmu and its autosync job (config in ~/.claude-multi-usage kept)"
"""


def _ssh(host: str, args: list[str], input_bytes: bytes, ssh_options: list[str]) -> None:
    cmd = ["ssh", *ssh_options, host, *args]
    subprocess.run(cmd, input=input_bytes, check=True)


def install(host: str, server_url: str, keys: list[tuple[str, str]], every_minutes: int,
            alias: str | None = None, source: str | None = None, python: str = "python3",
            ssh_options: list[str] | None = None, run=_ssh) -> list[str]:
    """Install cmu on ``host``. Returns lines describing what was done."""
    ssh_options = ssh_options or []
    source = source or default_source()
    src_path = Path(source).expanduser()
    lines = []
    if src_path.is_dir() and (src_path / "pyproject.toml").exists():
        tarball = make_tarball(src_path)
        unpack = f"rm -rf {REMOTE_BASE}/src && mkdir -p {REMOTE_BASE} && tar -xzf - -C {REMOTE_BASE}"
        run(host, ["bash", "-c", shlex.quote(unpack)], tarball, ssh_options)
        script = install_script(True, None, server_url, keys, alias, every_minutes, python)
        lines.append(f"sent this checkout ({len(tarball) // 1024} KB) to {host}:{REMOTE_BASE}/src")
    else:
        script = install_script(False, source, server_url, keys, alias, every_minutes, python)
        lines.append(f"{host} installs from {source}")
    run(host, ["bash", "-s"], script.encode(), ssh_options)
    lines.append(f"cmu installed on {host} with autosync every {every_minutes} min")
    return lines


def uninstall(host: str, ssh_options: list[str] | None = None, run=_ssh) -> list[str]:
    run(host, ["bash", "-s"], uninstall_script().encode(), ssh_options or [])
    return [f"cmu removed from {host}"]
