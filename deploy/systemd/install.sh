#!/usr/bin/env bash
# Install or upgrade the claude-multi-usage sync server as a systemd service.
#
# Run from a checkout of this repository, on the Linux host that will run the
# server:
#
#   sudo deploy/systemd/install.sh              # install, or upgrade an existing install
#   sudo deploy/systemd/install.sh --uninstall  # remove the service and the venv
#
# What it does:
#   - installs uv into /usr/local/bin if no uv is found (https://astral.sh/uv)
#   - creates a system user "cmu" with no login shell
#   - creates a virtualenv in /opt/cmu/venv from uv.lock with the [server]
#     extra (needs network access to PyPI; uv downloads a Python into
#     /opt/cmu/python if the host has none that is 3.9 or newer)
#   - creates /var/lib/cmu for the SQLite database
#   - writes /etc/default/cmu-server (settings; created once, never overwritten)
#   - installs cmu-server.service, enables it and (re)starts it
#   - waits for /api/health to answer
#
# To upgrade: git pull, then re-run this script. The database is kept.
# Set UV=/path/to/uv to use a particular uv, PYTHON=3.12 (or a path) to pick
# the interpreter.
#
# The server has no authentication. Keep it on a private network.
set -euo pipefail

APP_USER=cmu
INSTALL_DIR=/opt/cmu
VENV="$INSTALL_DIR/venv"
DATA_DIR=/var/lib/cmu
DEFAULTS_FILE=/etc/default/cmu-server
UNIT_NAME=cmu-server.service
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$SCRIPT_DIR/$UNIT_NAME"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
UV_INSTALL_URL=https://astral.sh/uv/install.sh

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
    exit 0
fi
[[ -z "${1:-}" ]] || die "unknown argument: $1 (only --uninstall is supported)"

[[ -f "$REPO_ROOT/pyproject.toml" && -f "$REPO_ROOT/uv.lock" && -f "$UNIT_SRC" ]] \
    || die "run this script from a checkout of the repository (pyproject.toml and uv.lock not found at $REPO_ROOT)"

# uv: the one named in $UV, the one on root's PATH, the usual places, or the
# invoking user's own copy. Otherwise install it for the whole host.
find_uv() {
    local sudo_home="" candidate
    [[ -n "${SUDO_USER:-}" ]] && sudo_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    for candidate in "${UV:-}" "$(command -v uv 2>/dev/null || true)" /usr/local/bin/uv \
                     /root/.local/bin/uv "${sudo_home:+$sudo_home/.local/bin/uv}"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}
if ! UV="$(find_uv)"; then
    echo "==> Installing uv into /usr/local/bin ($UV_INSTALL_URL)"
    command -v curl >/dev/null 2>&1 \
        || die "curl is needed to download uv (apt install curl), or install uv yourself and set UV=/path/to/uv"
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

echo "==> Virtualenv in $VENV (from uv.lock)"
# The venv is built by root but used by the "cmu" user, so it must not share
# files with root's cache (copy, not hardlink) and a downloaded Python must
# live somewhere world-readable rather than under /root.
export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_LINK_MODE=copy
export UV_PYTHON_INSTALL_DIR="$INSTALL_DIR/python"
"$UV" sync --project "$REPO_ROOT" --locked --no-dev --no-editable --extra server --quiet \
    ${PYTHON:+--python "$PYTHON"}
[[ -x "$VENV/bin/cmu" ]] || die "installation failed: $VENV/bin/cmu not found"

echo "==> Settings in $DEFAULTS_FILE"
if [[ ! -f "$DEFAULTS_FILE" ]]; then
    cat > "$DEFAULTS_FILE" <<'EOT'
# Settings for cmu-server.service. Uncomment to change a value, then:
#   sudo systemctl restart cmu-server
#
# The API has no authentication. Bind to a LAN or VPN address and never
# expose it to the Internet.
#CMU_HOST=0.0.0.0
#CMU_PORT=8000
# The database directory must exist and be writable by the "cmu" user
# (install.sh creates /var/lib/cmu; for another location, create it and
# `chown cmu:cmu` it first).
#CMU_DB_PATH=/var/lib/cmu/server.db
# Largest accepted sync payload in bytes.
#CMU_MAX_BODY_BYTES=2097152
EOT
    chmod 644 "$DEFAULTS_FILE"
fi

echo "==> Service $UNIT_NAME"
install -m 644 "$UNIT_SRC" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null
systemctl restart "$UNIT_NAME"

# Probe the address the service actually binds to.
read -r HOST PORT < <( (set +u; [[ -f "$DEFAULTS_FILE" ]] && . "$DEFAULTS_FILE"; echo "${CMU_HOST:-0.0.0.0} ${CMU_PORT:-8000}") )
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
        echo "    clients: cmu config server http://<this-host>:$PORT"
        exit 0
    fi
    sleep 0.5
done
echo "error: the service was started but $HEALTH_URL did not answer within 10s" >&2
echo "       (systemctl is-active $UNIT_NAME: $(systemctl is-active "$UNIT_NAME" || true))" >&2
echo "       journalctl -u $UNIT_NAME -n 50" >&2
exit 1
