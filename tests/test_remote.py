import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_multi_usage import cli, config, remote

CHECKOUT = Path(remote.__file__).resolve().parent.parent


def _bash_syntax_ok(script: str) -> bool:
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    return subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode == 0


def test_default_source_is_this_checkout():
    assert (CHECKOUT / "pyproject.toml").exists()
    assert remote.default_source() == str(CHECKOUT)


def test_default_source_falls_back_to_repository_url(monkeypatch, tmp_path):
    monkeypatch.setattr(remote, "PACKAGE_DIR", tmp_path / "site-packages" / "claude_multi_usage")
    assert remote.default_source().startswith("git+https://github.com/")


def test_make_tarball_contains_the_package_without_junk(tmp_path):
    src = tmp_path / "checkout"
    shutil.copytree(CHECKOUT / "claude_multi_usage", src / "claude_multi_usage",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(CHECKOUT / "pyproject.toml", src / "pyproject.toml")
    shutil.copy(CHECKOUT / "README.md", src / "README.md")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (src / ".venv" / "lib").mkdir(parents=True)
    (src / ".venv" / "lib" / "big.so").write_bytes(b"\0" * 1000)
    (src / "claude_multi_usage" / "__pycache__").mkdir()
    (src / "claude_multi_usage" / "__pycache__" / "cli.pyc").write_bytes(b"\0")
    (src / "claude_multi_usage.egg-info").mkdir()
    (src / "claude_multi_usage.egg-info" / "PKG-INFO").write_text("x")

    data = remote.make_tarball(src)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "src/pyproject.toml" in names
    assert "src/claude_multi_usage/cli.py" in names
    assert not any(".git" in n or ".venv" in n or "__pycache__" in n or "egg-info" in n for n in names)
    assert len(data) < 2_000_000


def test_install_script_is_valid_bash_and_configures_everything():
    script = remote.install_script(True, None, "http://10.0.0.5:8000", [("k1", "team key"), ("k2", "")],
                                   alias="office box", every_minutes=7, python="python3.12")
    assert _bash_syntax_ok(script)
    assert "PY=python3.12" in script
    assert 'pip install --quiet --upgrade "$BASE/src"' in script
    assert '"$VENV/bin/cmu" config server http://10.0.0.5:8000' in script
    assert "config key add k1 'team key'" in script
    assert "config key add k2 ''" in script
    assert "config alias 'office box'" in script
    assert "autosync install --every 7" in script
    assert "autosync run --force" in script
    assert "sys.version_info >= (3, 9)" in script

    req = remote.install_script(False, "git+https://github.com/x/y.git", "http://s", [("k", "")], None, 15)
    assert _bash_syntax_ok(req)
    assert "pip install --quiet --upgrade git+https://github.com/x/y.git" in req
    assert "config alias" not in req
    assert _bash_syntax_ok(remote.uninstall_script())


def test_install_streams_checkout_then_runs_script(tmp_path):
    src = tmp_path / "co"
    (src / "claude_multi_usage").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    (src / "claude_multi_usage" / "__init__.py").write_text("")
    calls = []

    def fake_ssh(host, args, input_bytes, ssh_options):
        calls.append((host, args, input_bytes, ssh_options))

    lines = remote.install("alice@box", "http://s:8000", [("k1", "")], 15, alias="box",
                           source=str(src), ssh_options=["-p", "2222"], run=fake_ssh)
    assert len(calls) == 2
    host, args, tarball, opts = calls[0]
    assert host == "alice@box" and opts == ["-p", "2222"]
    assert args[:2] == ["bash", "-c"] and "tar -xzf - -C ~/.local/share/cmu" in args[2]
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        assert "src/pyproject.toml" in tar.getnames()
    host, args, script, _ = calls[1]
    assert args == ["bash", "-s"]
    assert b'config alias box' in script and b'"$BASE/src"' in script
    assert any("sent this checkout" in line for line in lines)


def test_install_from_requirement_sends_no_tarball():
    calls = []
    remote.install("box", "http://s", [("k", "")], 15, source="git+https://github.com/x/y.git",
                   run=lambda *a: calls.append(a))
    assert len(calls) == 1
    assert calls[0][1] == ["bash", "-s"]
    assert b"git+https://github.com/x/y.git" in calls[0][2]


def test_uninstall_runs_script():
    calls = []
    assert remote.uninstall("box", run=lambda *a: calls.append(a)) == ["cmu removed from box"]
    assert calls[0][1] == ["bash", "-s"] and b"autosync uninstall" in calls[0][2]


# -- CLI --------------------------------------------------------------------------

def test_cli_remote_install_uses_local_config_by_default(fake_home, monkeypatch):
    config.set_server_url("http://sync.invalid")
    config.add_key("k1", "team")
    seen = {}

    def fake_install(host, server_url, keys, every, alias=None, source=None, python="python3",
                     ssh_options=None):
        seen.update(host=host, server_url=server_url, keys=keys, every=every, alias=alias,
                    source=source, python=python, ssh_options=ssh_options)
        return ["done"]

    monkeypatch.setattr(remote, "install", fake_install)
    r = CliRunner().invoke(cli.main, ["remote", "install", "box", "--every", "5", "--alias", "lap",
                                      "--ssh-option", "-o StrictHostKeyChecking=accept-new"])
    assert r.exit_code == 0, r.output
    assert seen == {"host": "box", "server_url": "http://sync.invalid", "keys": [("k1", "team")],
                    "every": 5, "alias": "lap", "source": None, "python": "python3",
                    "ssh_options": ["-o", "StrictHostKeyChecking=accept-new"]}
    assert "done" in r.output


def test_cli_remote_install_overrides(fake_home, monkeypatch):
    seen = {}
    monkeypatch.setattr(remote, "install", lambda host, server_url, keys, every, **kw: seen.update(
        server_url=server_url, keys=keys, **kw) or [])
    r = CliRunner().invoke(cli.main, ["remote", "install", "box", "--server", "http://x", "--key", "a",
                                      "--key", "b", "--from", "git+https://g/r.git", "--python", "python3.11"])
    assert r.exit_code == 0, r.output
    assert seen["server_url"] == "http://x" and seen["keys"] == [("a", ""), ("b", "")]
    assert seen["source"] == "git+https://g/r.git" and seen["python"] == "python3.11"


def test_cli_remote_install_requires_config(fake_home):
    r = CliRunner().invoke(cli.main, ["remote", "install", "box"])
    assert r.exit_code == 1 and "No server URL" in r.output
    config.set_server_url("http://x")
    r = CliRunner().invoke(cli.main, ["remote", "install", "box"])
    assert r.exit_code == 1 and "No keys" in r.output


def test_cli_remote_install_reports_ssh_failure(fake_home, monkeypatch):
    config.set_server_url("http://x")
    config.add_key("k")

    def failing(*a, **k):
        raise subprocess.CalledProcessError(255, ["ssh"])

    monkeypatch.setattr(remote, "install", failing)
    r = CliRunner().invoke(cli.main, ["remote", "install", "box"])
    assert r.exit_code == 1 and "ssh to box failed (exit 255)" in r.output


def test_cli_remote_uninstall(fake_home, monkeypatch):
    monkeypatch.setattr(remote, "uninstall", lambda host, ssh_options=None: [f"cmu removed from {host}"])
    r = CliRunner().invoke(cli.main, ["remote", "uninstall", "box"])
    assert r.exit_code == 0 and "cmu removed from box" in r.output
