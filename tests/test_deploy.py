"""The server installer is only ever run as root on a VM; at least keep it parseable
and make sure it installs what the README says it installs."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "systemd" / "install.sh"
UNIT = REPO / "deploy" / "systemd" / "sandburn-server.service"


def test_install_sh_is_valid_bash():
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_install_sh_installs_from_the_lock_and_always_rebuilds_the_project():
    s = SCRIPT.read_text()
    assert "--locked" in s and "--no-dev" in s and "--no-editable" in s and "--extra server" in s
    # uv would otherwise keep the previous build of the project until pyproject.toml changes
    assert "--reinstall-package sandburn" in s
    assert "umask 022" in s
    assert "UV_LINK_MODE=copy" in s and "UV_PYTHON_INSTALL_DIR=" in s
    assert "pip install" not in s and "-m venv" not in s
    assert "ExecStart=/opt/sandburn/venv/bin/sburn" in UNIT.read_text()


def test_install_sh_takes_over_a_pre_rename_server():
    s = SCRIPT.read_text()
    assert 'systemctl disable --now "$LEGACY_UNIT"' in s and "LEGACY_UNIT=cmu-server.service" in s
    assert "LEGACY_DATA_DIR=/var/lib/cmu" in s and "LEGACY_INSTALL_DIR=/opt/cmu" in s
    assert "SANDBURN_*" in s  # the settings file is not converted; the user is told
