#!/usr/bin/env bash
# Install or upgrade the sandburn sync server as a systemd service.
#
# Run from a checkout of this repository, on the Linux host that will run the
# server:
#
#   sudo deploy/systemd/install.sh              # install, or upgrade an existing install
#   sudo deploy/systemd/install.sh --uninstall  # remove the service and the venv
#
# What it does:
#   - installs uv into /usr/local/bin if no uv is found (https://astral.sh/uv)
#   - creates a system user "sandburn" with no login shell
#   - creates a virtualenv in /opt/sandburn/venv from uv.lock with the [server]
#     extra, on the host's Python 3.13 or one uv downloads into
#     /opt/sandburn/python (needs network access to PyPI)
#   - creates /var/lib/sandburn for the SQLite database
#   - writes /etc/default/sandburn-server (settings; created once, never overwritten)
#   - installs sandburn-server.service, enables it and (re)starts it
#   - waits for /api/health to answer
#
# To upgrade: git pull, then re-run this script. The database is kept.
# Variables (sudo resets the environment, so pass them on the command line,
# e.g. `sudo PYTHON=3.12 deploy/systemd/install.sh`):
#   UV=/path/to/uv   use this uv instead of searching for one or installing it
#   PYTHON=3.12      Python version or interpreter path for the venv (default 3.13)
#
# The server has no authentication. Keep it on a private network.
set -euo pipefail
umask 022
# A predictable PATH: a root login shell may carry /root/.local/bin, where a
# uv-managed Python would be found first and end up, unreadable by the
# service user, as the venv's interpreter.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

APP_USER=sandburn
INSTALL_DIR=/opt/sandburn
VENV="$INSTALL_DIR/venv"
DATA_DIR=/var/lib/sandburn
DEFAULTS_FILE=/etc/default/sandburn-server
UNIT_NAME=sandburn-server.service
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$SCRIPT_DIR/$UNIT_NAME"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
UV_INSTALL_URL=https://astral.sh/uv/install.sh
PYTHON="${PYTHON:-3.13}"

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this script is for hosts running systemd"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT_NAME"
    systemctl daemon-reload
    rm -rf "$INSTALL_DIR"
    echo "Removed $UNIT_NAME and $INSTALL_DIR."
    echo "Kept the database in $DATA_DIR, the settings in $DEFAULTS_FILE, the '$APP_USER' user and uv."
    echo "To remove those too:"
    echo "  sudo rm -rf $DATA_DIR $DEFAULTS_FILE && sudo userdel $APP_USER"
    echo "  sudo rm -rf /root/.cache/uv                        # uv's download cache from the install"
    echo "  sudo rm -f /usr/local/bin/uv /usr/local/bin/uvx    # if this script installed uv"
    exit 0
fi
[[ -z "${1:-}" ]] || die "unknown argument: $1 (only --uninstall is supported)"

[[ -f "$REPO_ROOT/pyproject.toml" && -f "$REPO_ROOT/uv.lock" && -f "$UNIT_SRC" ]] \
    || die "run this script from a checkout of the repository (pyproject.toml and uv.lock not found at $REPO_ROOT)"

# uv: the one named in $UV, the one on PATH, the usual places, or the invoking
# user's own copy. Otherwise install it for the whole host.
if [[ -n "${UV:-}" ]] && ! [[ -f "$UV" && -x "$UV" ]]; then
    die "UV=$UV is not an executable"
fi
find_uv() {
    local sudo_home="" candidate
    [[ -n "${SUDO_USER:-}" ]] && sudo_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    for candidate in "${UV:-}" "$(command -v uv 2>/dev/null || true)" /usr/local/bin/uv \
                     /root/.local/bin/uv "${sudo_home:+$sudo_home/.local/bin/uv}"; do
        if [[ -n "$candidate" && -f "$candidate" && -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}
if ! UV="$(find_uv)"; then
    echo "==> Installing uv into /usr/local/bin ($UV_INSTALL_URL)"
    command -v curl >/dev/null 2>&1 \
        || die "curl is needed to download uv (apt install curl), or install uv yourself and pass UV=/path/to/uv"
    curl -LsSf "$UV_INSTALL_URL" | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
    UV=/usr/local/bin/uv
    [[ -x "$UV" ]] || die "uv installation failed: $UV not found"
fi
echo "==> Using $UV ($("$UV" --version))"

echo "==> System user and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
    useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell "$NOLOGIN" "$APP_USER"
fi
install -d -m 755 "$INSTALL_DIR"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

# An install from before the tool was renamed (cmu-server.service, /opt/cmu,
# /var/lib/cmu, the "cmu" user): stop it and take over its database.
LEGACY_UNIT=cmu-server.service
LEGACY_INSTALL_DIR=/opt/cmu
LEGACY_DATA_DIR=/var/lib/cmu
LEGACY_DEFAULTS_FILE=/etc/default/cmu-server
if [[ -f "/etc/systemd/system/$LEGACY_UNIT" ]]; then
    echo "==> Retiring $LEGACY_UNIT"
    systemctl disable --now "$LEGACY_UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/$LEGACY_UNIT"
    systemctl daemon-reload
fi
if [[ -f "$LEGACY_DATA_DIR/server.db" && ! -e "$DATA_DIR/server.db" ]]; then
    echo "==> Moving the database from $LEGACY_DATA_DIR to $DATA_DIR"
    for f in "$LEGACY_DATA_DIR/server.db" "$LEGACY_DATA_DIR/server.db-wal" "$LEGACY_DATA_DIR/server.db-shm"; do
        if [[ -e "$f" ]]; then mv "$f" "$DATA_DIR/"; fi
    done
    chown "$APP_USER:$APP_USER" "$DATA_DIR"/server.db*
fi
rm -rf "$LEGACY_INSTALL_DIR"
if [[ -f "$LEGACY_DEFAULTS_FILE" ]] && grep -qE '^[[:space:]]*CMU_' "$LEGACY_DEFAULTS_FILE"; then
    echo "note: these settings in $LEGACY_DEFAULTS_FILE are not carried over; set them as SANDBURN_* in $DEFAULTS_FILE:"
    grep -E '^[[:space:]]*CMU_' "$LEGACY_DEFAULTS_FILE" | sed 's/^/      /'
fi

echo "==> Virtualenv in $VENV (from uv.lock, Python $PYTHON)"
# The venv is built by root but used by the "sandburn" user, so it must not share
# files with root's cache (copy, not hardlink) and a downloaded Python must
# live somewhere world-readable rather than under /root. The project itself
# is always rebuilt: uv would otherwise keep the previous build until
# pyproject.toml changes, and `git pull` rarely changes it.
export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_LINK_MODE=copy
export UV_PYTHON_INSTALL_DIR="$INSTALL_DIR/python"
# uv keeps an existing venv whose interpreter satisfies the version request,
# so one built on a Python under /root or /home (unreadable for the service
# user) has to go first.
if [[ -e "$VENV/bin/python" ]]; then
    case "$(readlink -f "$VENV/bin/python")" in
        /root/*|/home/*)
            echo "==> Recreating $VENV: its Python, $(readlink -f "$VENV/bin/python"), is not readable by '$APP_USER'"
            rm -rf "${VENV:?}" ;;
    esac
fi
"$UV" sync --project "$REPO_ROOT" --locked --no-dev --no-editable --extra server --quiet \
    --reinstall-package sandburn --python "$PYTHON"
[[ -x "$VENV/bin/sburn" ]] || die "installation failed: $VENV/bin/sburn not found"
# Copied files keep the modes they had in root's cache; make sure the service
# user can read and run everything, then prove it.
chmod -R u=rwX,go=rX "$VENV"
[[ -d "$INSTALL_DIR/python" ]] && chmod -R u=rwX,go=rX "$INSTALL_DIR/python"
case "$(readlink -f "$VENV/bin/python")" in
    /root/*|/home/*) die "the venv uses $(readlink -f "$VENV/bin/python"), which the '$APP_USER' user cannot read; pass PYTHON=<version> or an interpreter path outside /root and /home" ;;
esac
runuser -u "$APP_USER" -- env HOME="$DATA_DIR" "$VENV/bin/sburn" --version >/dev/null \
    || die "the '$APP_USER' user cannot run $VENV/bin/sburn (interpreter: $(readlink -f "$VENV/bin/python")); check the permissions under $INSTALL_DIR"

echo "==> Settings in $DEFAULTS_FILE"
if [[ ! -f "$DEFAULTS_FILE" ]]; then
    cat > "$DEFAULTS_FILE" <<'EOT'
# Settings for sandburn-server.service. Uncomment to change a value, then:
#   sudo systemctl restart sandburn-server
#
# The API has no authentication. Bind to a LAN or VPN address and never
# expose it to the Internet.
#SANDBURN_HOST=0.0.0.0
#SANDBURN_PORT=8000
# The database directory must exist and be writable by the "sandburn" user
# (install.sh creates /var/lib/sandburn; for another location, create it and
# `chown sandburn:sandburn` it first).
#SANDBURN_DB_PATH=/var/lib/sandburn/server.db
# Largest accepted sync payload in bytes.
#SANDBURN_MAX_BODY_BYTES=2097152
EOT
    chmod 644 "$DEFAULTS_FILE"
fi

echo "==> Service $UNIT_NAME"
install -m 644 "$UNIT_SRC" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null
systemctl restart "$UNIT_NAME"

# Probe the address the service actually binds to.
read -r HOST PORT < <( (set +u; [[ -f "$DEFAULTS_FILE" ]] && . "$DEFAULTS_FILE"; echo "${SANDBURN_HOST:-0.0.0.0} ${SANDBURN_PORT:-8000}") )
case "$HOST" in
    ""|0.0.0.0) PROBE_HOST=127.0.0.1 ;;
    ::|"[::]")  PROBE_HOST="[::1]" ;;
    *:*)        PROBE_HOST="[$HOST]" ;;   # bare IPv6 address
    *)          PROBE_HOST="$HOST" ;;
esac
HEALTH_URL="http://$PROBE_HOST:$PORT/api/health"
for _ in $(seq 1 20); do
    if "$VENV/bin/python" -c "import urllib.request; urllib.request.urlopen('$HEALTH_URL', timeout=1)" 2>/dev/null; then
        echo "==> Running: $HEALTH_URL is up"
        echo "    status:  systemctl status $UNIT_NAME"
        echo "    logs:    journalctl -u $UNIT_NAME -f"
        echo "    clients: sburn config server http://<this-host>:$PORT"
        if id -u cmu >/dev/null 2>&1 || [[ -e "$LEGACY_DATA_DIR" || -e "$LEGACY_DEFAULTS_FILE" ]]; then
            echo "    pre-rename leftovers, once you are happy: sudo userdel cmu; sudo rm -rf $LEGACY_DATA_DIR $LEGACY_DEFAULTS_FILE"
        fi
        exit 0
    fi
    sleep 0.5
done
echo "error: the service was started but $HEALTH_URL did not answer within 10s" >&2
echo "       (systemctl is-active $UNIT_NAME: $(systemctl is-active "$UNIT_NAME" || true))" >&2
echo "       journalctl -u $UNIT_NAME -n 50" >&2
exit 1
