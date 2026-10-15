import json
import plistlib
import shutil
import subprocess
import threading
import urllib.request

import pytest
from click.testing import CliRunner

from claude_multi_usage import autosync, cli, config


@pytest.fixture
def auto_env(fake_home, monkeypatch):
    cmu_dir = fake_home.home / ".claude-multi-usage"
    monkeypatch.setattr(autosync, "CACHE_DIR", cmu_dir)
    monkeypatch.setattr(autosync, "STATE_FILE", cmu_dir / "autosync-state.json")
    monkeypatch.setattr(autosync, "LOCK_FILE", cmu_dir / "autosync.lock")
    monkeypatch.setattr(autosync, "systemd_user_dir", lambda: fake_home.home / "systemd-user")
    monkeypatch.setattr(autosync, "launch_agents_dir", lambda: fake_home.home / "LaunchAgents")
    monkeypatch.setattr(autosync, "_timer_stamp", lambda: fake_home.home / "timers" / "stamp-cmu-autosync.timer")
    calls = []
    results = {}

    def fake_run(cmd, check=True):
        calls.append(cmd)
        rc, out = results.get(tuple(cmd[:3]), (0, "enabled\n"))
        if rc and check:
            raise subprocess.CalledProcessError(rc, cmd, output="", stderr=out)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    monkeypatch.setattr(autosync, "_run", fake_run)
    fake_home.calls = calls
    fake_home.results = results
    return fake_home


# -- change detection ---------------------------------------------------------

def test_fingerprint_tracks_usage_files_and_settings(auto_env):
    before = autosync.fingerprint()
    assert before == autosync.fingerprint()  # stable when nothing changed
    path = auto_env.projects / "-home-alice-workspace-myproj" / "aaaa.jsonl"
    with open(path, "a") as f:
        f.write("{}\n")
    after_append = autosync.fingerprint()
    assert after_append != before
    (auto_env.projects / "-home-alice-workspace-myproj" / "new.jsonl").write_text("{}\n")
    after_new = autosync.fingerprint()
    assert after_new != after_append
    (auto_env.home / ".claude" / "stats-cache.json").write_text("{}")
    after_stats = autosync.fingerprint()
    assert after_stats not in (before, after_append, after_new)
    # a new server, key or alias must be pushed too
    config.set_server_url("http://new.invalid")
    after_server = autosync.fingerprint()
    assert after_server != after_stats
    config.add_key("k2")
    after_key = autosync.fingerprint()
    assert after_key != after_server
    config.set_alias("box")
    assert autosync.fingerprint() != after_key


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
    synced = []
    assert autosync.run(lambda: synced.append(1)) == "synced"


def test_run_is_single_flight(auto_env):
    """A second run while one is in progress reports busy instead of racing."""
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)

    t = threading.Thread(target=autosync.run, args=(slow,))
    t.start()
    assert started.wait(5)
    assert autosync.run(lambda: None) == "busy"
    release.set()
    t.join(5)
    assert autosync.run(lambda: None) == "unchanged"


def test_record_success_marks_current_state(auto_env):
    autosync.record_success()
    assert autosync.run(lambda: pytest.fail("should not sync")) == "unchanged"


def test_state_survives_corrupt_file(auto_env):
    autosync.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    autosync.STATE_FILE.write_text("{broken")
    assert autosync.load_state() == {}
    assert autosync.run(lambda: None) == "synced"
    assert json.loads(autosync.STATE_FILE.read_text())["last_result"] == "synced"
    assert not list(autosync.STATE_FILE.parent.glob("*.tmp"))


# -- systemd backend ----------------------------------------------------------

def test_install_systemd_writes_units_and_enables_timer(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    monkeypatch.setattr(autosync, "_command", lambda: ["/opt/my venv/bin/cmu", "autosync", "run", "--quiet"])
    lines = autosync.install(every_minutes=7)
    unit_dir = autosync.systemd_user_dir()
    service = (unit_dir / "cmu-autosync.service").read_text()
    timer = (unit_dir / "cmu-autosync.timer").read_text()
    assert 'ExecStart="/opt/my venv/bin/cmu" autosync run --quiet' in service
    assert "Type=oneshot" in service and "WorkingDirectory=" in service
    assert "OnActiveSec=1min" in timer and "OnUnitActiveSec=7min" in timer
    assert "OnBootSec" not in timer and "Persistent" not in timer  # see the comment in the unit
    assert "WantedBy=timers.target" in timer
    assert auto_env.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "cmu-autosync.timer"],
    ]
    assert any("every 7 min" in line for line in lines)
    assert any("enable-linger" in line for line in lines)

    info = autosync.status()
    assert info["installed"] and info["enabled"] == "enabled"
    assert "installed: yes" in "\n".join(autosync.describe_status(info))

    stamp = autosync._timer_stamp()
    stamp.parent.mkdir(parents=True)
    stamp.write_text("")
    removed = autosync.uninstall()
    assert not (unit_dir / "cmu-autosync.timer").exists()
    assert not stamp.exists()
    assert any("removed" in line for line in removed)
    assert ["systemctl", "--user", "disable", "--now", "cmu-autosync.timer"] in auto_env.calls
    assert ["systemctl", "--user", "clean", "--what=state", "cmu-autosync.timer"] in auto_env.calls
    assert not autosync.status()["installed"]


def test_systemd_quoting_escapes_specifiers():
    assert autosync._quote_systemd("/opt/50%off/$HOME x") == '"/opt/50%%off/$$HOME x"'
    assert autosync._quote_systemd("/plain/path") == "/plain/path"


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
def test_generated_units_pass_systemd_analyze(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    autosync.install(every_minutes=3)
    unit_dir = autosync.systemd_user_dir()
    res = subprocess.run(["systemd-analyze", "--user", "verify", str(unit_dir / "cmu-autosync.timer"),
                          str(unit_dir / "cmu-autosync.service")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + res.stdout


def test_install_systemd_linger(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    lines = autosync.install(every_minutes=15, linger=True)
    assert ["loginctl", "enable-linger"] in auto_env.calls
    assert any("lingering enabled" in line for line in lines)


def test_failed_systemctl_leaves_nothing_behind(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    auto_env.results[("systemctl", "--user", "daemon-reload")] = (1, "Failed to connect to bus")
    with pytest.raises(subprocess.CalledProcessError):
        autosync.install()
    assert not (autosync.systemd_user_dir() / "cmu-autosync.timer").exists()
    assert not autosync.status()["installed"]


def test_install_rejects_bad_interval(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "systemd")
    with pytest.raises(ValueError):
        autosync.install(every_minutes=0)


def test_systemd_env_is_filled_in_for_non_login_shells(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(autosync.os, "getuid", lambda: 1234)
    env = autosync._systemd_env()
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1234"
    assert "DBUS_SESSION_BUS_ADDRESS" not in env  # no bus socket there
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "bus").write_text("")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert autosync._systemd_env()["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"


def test_systemd_user_dir_ignores_empty_or_relative_xdg(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert autosync.systemd_user_dir().is_absolute()
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert autosync.systemd_user_dir().is_absolute()
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom")
    assert str(autosync.systemd_user_dir()) == "/custom/systemd/user"


# -- launchd backend ----------------------------------------------------------

def test_install_launchd_writes_plist_into_gui_domain(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "launchd")
    monkeypatch.setattr(autosync.os, "getuid", lambda: 501)
    auto_env.results[("launchctl", "print", "gui/501/com.claude-multi-usage.autosync")] = (113, "not found")
    lines = autosync.install(every_minutes=10)
    plist = autosync.launch_agents_dir() / "com.claude-multi-usage.autosync.plist"
    with open(plist, "rb") as f:
        data = plistlib.load(f)
    assert data["ProgramArguments"][-3:] == ["autosync", "run", "--quiet"]
    assert data["StartInterval"] == 600 and data["RunAtLoad"] is True
    assert data["WorkingDirectory"] == str(autosync.CACHE_DIR)
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in auto_env.calls
    assert any("every 10 min" in line for line in lines) and any("loaded into gui/501" in line for line in lines)
    assert autosync.status()["installed"]
    autosync.uninstall()
    assert not plist.exists()


def test_install_launchd_without_console_login(auto_env, monkeypatch):
    """Over ssh there is no gui/<uid> domain; use user/<uid> and never fail."""
    monkeypatch.setattr(autosync, "backend", lambda: "launchd")
    monkeypatch.setattr(autosync.os, "getuid", lambda: 501)
    auto_env.results[("launchctl", "print", "gui/501")] = (1, "Could not find domain")
    auto_env.results[("launchctl", "print", "user/501/com.claude-multi-usage.autosync")] = (113, "")
    auto_env.results[("launchctl", "bootstrap", "user/501")] = (5, "Input/output error")
    lines = autosync.install(every_minutes=10)
    assert (autosync.launch_agents_dir() / "com.claude-multi-usage.autosync.plist").exists()
    assert any("starts at the next login" in line for line in lines)
    assert ["launchctl", "bootstrap", "user/501",
            str(autosync.launch_agents_dir() / "com.claude-multi-usage.autosync.plist")] in auto_env.calls


def test_unsupported_platform(auto_env, monkeypatch):
    monkeypatch.setattr(autosync, "backend", lambda: "unsupported")
    with pytest.raises(RuntimeError, match="no supported scheduler"):
        autosync.install()
    assert autosync.uninstall() == ["nothing to remove: no supported scheduler on this system"]


def test_backend_detection(monkeypatch, tmp_path):
    monkeypatch.setattr(autosync.platform, "system", lambda: "Linux")
    monkeypatch.setattr(autosync.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(autosync, "SYSTEMD_BOOTED_DIR", tmp_path / "missing")
    assert autosync.backend() == "unsupported"  # systemctl present but systemd not running (containers, WSL)
    monkeypatch.setattr(autosync, "SYSTEMD_BOOTED_DIR", tmp_path)
    assert autosync.backend() == "systemd"
    monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
    assert autosync.backend() == "unsupported"
    monkeypatch.setattr(autosync.platform, "system", lambda: "Darwin")
    assert autosync.backend() == "launchd"
    monkeypatch.setattr(autosync.platform, "system", lambda: "Windows")
    assert autosync.backend() == "unsupported"


def test_scheduler_prefers_the_console_script(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "python"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(autosync.sys, "executable", str(exe))
    assert autosync._command() == [str(exe), "-m", "claude_multi_usage.cli", "autosync", "run", "--quiet"]
    (exe.parent / "cmu").write_text("")
    assert autosync._command() == [str(exe.parent / "cmu"), "autosync", "run", "--quiet"]


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


def test_plain_sync_records_the_fingerprint(auto_env, monkeypatch):
    posted = []
    _configure(monkeypatch, posted)
    runner = CliRunner()
    assert runner.invoke(cli.main, ["sync"]).exit_code == 0
    assert "Nothing changed" in runner.invoke(cli.main, ["sync", "--if-changed"]).output
    assert len(posted) == 1


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
    auto_env.results[("systemctl", "--user", "daemon-reload")] = (1, "Failed to connect to bus")
    r = CliRunner().invoke(cli.main, ["autosync", "install"])
    assert r.exit_code == 1 and "Failed to connect to bus" in r.output
