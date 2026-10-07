import json
import os
import plistlib
import subprocess
import urllib.request

import pytest
from click.testing import CliRunner

from claude_multi_usage import autosync, cli, config


@pytest.fixture
def auto_env(fake_home, monkeypatch):
    cmu_dir = fake_home.home / ".claude-multi-usage"
    monkeypatch.setattr(autosync, "CACHE_DIR", cmu_dir)
    monkeypatch.setattr(autosync, "STATE_FILE", cmu_dir / "autosync-state.json")
    monkeypatch.setattr(autosync, "systemd_user_dir", lambda: fake_home.home / "systemd-user")
    monkeypatch.setattr(autosync, "launch_agents_dir", lambda: fake_home.home / "LaunchAgents")
    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="enabled\n", stderr="")

    monkeypatch.setattr(autosync, "_run", fake_run)
    fake_home.calls = calls
    return fake_home


# -- change detection ---------------------------------------------------------

def test_fingerprint_tracks_usage_files(auto_env):
    before = autosync.fingerprint()
    assert before == autosync.fingerprint()  # stable when nothing changed
    path = auto_env.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    with open(path, "a") as f:
        f.write("{}\n")
    after_append = autosync.fingerprint()
    assert after_append != before
    (auto_env.projects / "-home-alice-workspace-myproj" / "new.jsonl").write_text("{}\n")
    assert autosync.fingerprint() != after_append
    (auto_env.home / ".claude" / "stats-cache.json").write_text("{}")
    assert autosync.fingerprint() not in (before, after_append)


def test_run_syncs_only_when_changed(auto_env):
    synced = []
    assert autosync.run(lambda: synced.append(1)) == "synced"
    assert autosync.run(lambda: synced.append(1)) == "unchanged"
    assert synced == [1]
    state = autosync.load_state()
    assert state["last_result"] == "unchanged" and state["last_sync"] and state["fingerprint"]

    with open(auto_env.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl", "a") as f:
        f.write("{}\n")
    assert autosync.run(lambda: synced.append(1)) == "synced"
    assert synced == [1, 1]
    assert autosync.run(lambda: synced.append(1), force=True) == "synced"
    assert synced == [1, 1, 1]


def test_run_records_failure_and_does_not_advance(auto_env):
    def boom():
        raise RuntimeError("server down")

    with pytest.raises(RuntimeError):
        autosync.run(boom)
    state = autosync.load_state()
    assert state["last_result"] == "failed: server down"
    assert "fingerprint" not in state
    # next run retries because nothing was recorded as synced
    synced = []
    assert autosync.run(lambda: synced.append(1)) == "synced"


def test_state_survives_corrupt_file(auto_env):
    autosync.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    autosync.STATE_FILE.write_text("{broken")
    assert autosync.load_state() == {}
    assert autosync.run(lambda: None) == "synced"
    assert json.loads(autosync.STATE_FILE.read_text())["last_result"] == "synced"


# -- systemd backend ----------------------------------------------------------

def test_install_systemd_writes_units_and_enables_timer(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    monkeypatch.setattr(autosync.sys, "executable", "/opt/my venv/bin/python")
    lines = autosync.install(every_minutes=7)
    unit_dir = autosync.systemd_user_dir()
    service = (unit_dir / "cmu-autosync.service").read_text()
    timer = (unit_dir / "cmu-autosync.timer").read_text()
    assert 'ExecStart="/opt/my venv/bin/python" -m claude_multi_usage.cli autosync run --quiet' in service
    assert "Type=oneshot" in service
    assert "OnUnitActiveSec=7min" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer
    assert auto_env.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "cmu-autosync.timer"],
    ]
    assert any("every 7 min" in line for line in lines)
    assert any("enable-linger" in line for line in lines)

    info = autosync.status()
    assert info["installed"] and info["enabled"] == "enabled"
    assert "installed: yes" in "\n".join(autosync.describe_status(info))

    removed = autosync.uninstall()
    assert not (unit_dir / "cmu-autosync.timer").exists()
    assert any("removed" in line for line in removed)
    assert ["systemctl", "--user", "disable", "--now", "cmu-autosync.timer"] in auto_env.calls
    assert not autosync.status()["installed"]


def test_install_systemd_linger(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    lines = autosync.install(every_minutes=15, linger=True)
    assert ["loginctl", "enable-linger"] in auto_env.calls
    assert any("lingering enabled" in line for line in lines)


def test_install_rejects_bad_interval(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    with pytest.raises(ValueError):
        autosync.install(every_minutes=0)


# -- launchd backend ----------------------------------------------------------

def test_install_launchd_writes_plist(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "launchd")
    monkeypatch.setattr(autosync.os, "getuid", lambda: 501)
    lines = autosync.install(every_minutes=10)
    plist = autosync.launch_agents_dir() / "com.claude-multi-usage.autosync.plist"
    with open(plist, "rb") as f:
        data = plistlib.load(f)
    assert data["ProgramArguments"][1:] == ["-m", "claude_multi_usage.cli", "autosync", "run", "--quiet"]
    assert data["StartInterval"] == 600 and data["RunAtLoad"] is True
    assert auto_env.calls[-1] == ["launchctl", "bootstrap", "gui/501", str(plist)]
    assert any("every 10 min" in line for line in lines)
    assert autosync.status()["installed"]
    autosync.uninstall()
    assert not plist.exists()


def test_unsupported_platform(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "unsupported")
    with pytest.raises(RuntimeError, match="no supported scheduler"):
        autosync.install()
    assert autosync.uninstall() == ["nothing to remove: no supported scheduler on this system"]


def test_backend_detection(monkeypatch):
    monkeypatch.setattr(autosync.platform, "system", lambda: "Linux")
    monkeypatch.setattr(autosync.shutil, "which", lambda name: "/usr/bin/systemctl")
    assert autosync.backend() == "systemd"
    monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
    assert autosync.backend() == "unsupported"
    monkeypatch.setattr(autosync.platform, "system", lambda: "Darwin")
    assert autosync.backend() == "launchd"
    monkeypatch.setattr(autosync.platform, "system", lambda: "Windows")
    assert autosync.backend() == "unsupported"


# -- CLI ------------------------------------------------------------------------

class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"status":"ok"}'


def _configure(monkeypatch, posted):
    config.set_server_url("http://sync.invalid")
    config.add_key("k1")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: posted.append(json.loads(req.data)) or _Resp())


def test_cli_sync_if_changed(auto_env, monkeypatch):
    posted = []
    _configure(monkeypatch, posted)
    runner = CliRunner()
    r = runner.invoke(cli.main, ["sync", "--if-changed"])
    assert r.exit_code == 0 and "Synced" in r.output
    r = runner.invoke(cli.main, ["sync", "--if-changed"])
    assert r.exit_code == 0 and "Nothing changed" in r.output
    assert len(posted) == 1
    with open(auto_env.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl", "a") as f:
        f.write("{}\n")
    r = runner.invoke(cli.main, ["sync", "--if-changed", "--quiet"])
    assert r.exit_code == 0 and r.output == ""
    assert len(posted) == 2


def test_cli_autosync_run(auto_env, monkeypatch):
    posted = []
    _configure(monkeypatch, posted)
    runner = CliRunner()
    assert "Synced at" in runner.invoke(cli.main, ["autosync", "run"]).output
    assert "Nothing changed" in runner.invoke(cli.main, ["autosync", "run"]).output
    assert runner.invoke(cli.main, ["autosync", "run", "--quiet"]).output == ""
    assert "Synced at" in runner.invoke(cli.main, ["autosync", "run", "--force"]).output
    assert len(posted) == 2
    assert "result:    synced" in runner.invoke(cli.main, ["autosync", "status"]).output


def test_cli_autosync_run_without_config(auto_env):
    r = CliRunner().invoke(cli.main, ["autosync", "run"])
    assert r.exit_code == 1 and "Server URL not configured" in r.output
    assert autosync.load_state()["last_result"].startswith("failed")


def test_cli_autosync_install_and_uninstall(auto_env, monkeypatch):
    posted = []
    _configure(monkeypatch, posted)
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    runner = CliRunner()
    r = runner.invoke(cli.main, ["autosync", "install", "--every", "5"])
    assert r.exit_code == 0, r.output
    assert "every 5 min" in r.output and "First sync: synced" in r.output
    assert len(posted) == 1
    assert "installed: yes" in runner.invoke(cli.main, ["autosync", "status"]).output
    r = runner.invoke(cli.main, ["autosync", "uninstall"])
    assert r.exit_code == 0 and "removed" in r.output


def test_cli_autosync_install_needs_config(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    r = CliRunner().invoke(cli.main, ["autosync", "install"])
    assert r.exit_code == 1 and "Server URL not configured" in r.output
    assert not (autosync.systemd_user_dir() / "cmu-autosync.timer").exists()


def test_cli_autosync_install_reports_systemctl_failure(auto_env, monkeypatch):
    posted = []
    _configure(monkeypatch, posted)
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")

    def failing_run(cmd, check=True):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="Failed to connect to bus")

    monkeypatch.setattr(autosync, "_run", failing_run)
    r = CliRunner().invoke(cli.main, ["autosync", "install"])
    assert r.exit_code == 1 and "Failed to connect to bus" in r.output


def test_scheduler_command_uses_this_interpreter():
    cmd = autosync._command()
    assert cmd[0] == os.path.realpath(cmd[0]) or os.path.exists(cmd[0])
    assert cmd[1:] == ["-m", "claude_multi_usage.cli", "autosync", "run", "--quiet"]
