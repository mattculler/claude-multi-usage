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
#   - creates a system user "cmu" with no login shell
#   - creates a virtualenv in /opt/cmu/venv and installs this checkout into it
#     with the [server] extra (needs network access to PyPI)
#   - creates /var/lib/cmu for the SQLite database
#   - writes /etc/default/cmu-server (settings; created once, never overwritten)
#   - installs cmu-server.service, enables it and (re)starts it
#
# To upgrade: git pull, then re-run this script. The database is kept.
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
PYTHON="${PYTHON:-python3}"

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this script is for hosts running systemd"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT_NAME"
    systemctl daemon-reload
    rm -rf "$INSTALL_DIR"
    echo "Removed $UNIT_NAME and $INSTALL_DIR."
    echo "Kept the database in $DATA_DIR, the settings in $DEFAULTS_FILE and the '$APP_USER' user."
    echo "To remove those too:"
    echo "  sudo rm -rf $DATA_DIR $DEFAULTS_FILE && sudo userdel $APP_USER"
    exit 0
fi
[[ -z "${1:-}" ]] || die "unknown argument: $1 (only --uninstall is supported)"

[[ -f "$REPO_ROOT/pyproject.toml" && -f "$UNIT_SRC" ]] \
    || die "run this script from a checkout of the repository (pyproject.toml not found at $REPO_ROOT)"
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found; need Python 3.9+ (set PYTHON=/path/to/python3 to choose one)"
"$PYTHON" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
    || die "$PYTHON must be Python 3.9+ with the venv module (Debian/Ubuntu: apt install python3-venv)"

echo "==> System user and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
    useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell "$NOLOGIN" "$APP_USER"
fi
install -d -m 755 "$INSTALL_DIR"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

echo "==> Virtualenv in $VENV"
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "$REPO_ROOT[server]"
[[ -x "$VENV/bin/cmu" ]] || die "installation failed: $VENV/bin/cmu not found"

echo "==> Settings in $DEFAULTS_FILE"
if [[ ! -f "$DEFAULTS_FILE" ]]; then
    cat > "$DEFAULTS_FILE" <<'EOF'
# Settings for cmu-server.service. Uncomment to change a value, then:
#   sudo systemctl restart cmu-server
#
# The API has no authentication. Bind to a LAN or VPN address and never
# expose it to the Internet.
#CMU_HOST=0.0.0.0
#CMU_PORT=8000
#CMU_DB_PATH=/var/lib/cmu/server.db
#CMU_MAX_BODY_BYTES=2097152
EOF
    chmod 644 "$DEFAULTS_FILE"
fi

echo "==> Service $UNIT_NAME"
install -m 644 "$UNIT_SRC" "/etc/systemd/system/$UNIT_NAME"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null
systemctl restart "$UNIT_NAME"

PORT="$( (set +u; [[ -f "$DEFAULTS_FILE" ]] && . "$DEFAULTS_FILE"; echo "${CMU_PORT:-8000}") )"
for _ in $(seq 1 20); do
    if "$VENV/bin/python" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=1)" 2>/dev/null; then
        echo "==> Running: http://127.0.0.1:$PORT/api/health is up"
        echo "    status:  systemctl status $UNIT_NAME"
        echo "    logs:    journalctl -u $UNIT_NAME -f"
        echo "    clients: cmu config server http://<this-host>:$PORT"
        exit 0
    fi
    sleep 0.5
done
echo "warning: the service was started but /api/health did not answer within 10s." >&2
echo "         journalctl -u $UNIT_NAME -n 50" >&2
exit 1
