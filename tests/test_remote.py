import io
import os
import shutil
import subprocess
import sys
import tarfile
import types
from importlib import metadata
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_multi_usage import cli, config, remote

CHECKOUT = Path(remote.__file__).resolve().parent.parent


def _bash_syntax_ok(script: str) -> bool:
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    return (
        subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode == 0
    )


def _make_checkout(tmp_path, git=False):
    src = tmp_path / "checkout"
    shutil.copytree(
        CHECKOUT / "claude_multi_usage",
        src / "claude_multi_usage",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(CHECKOUT / "pyproject.toml", src / "pyproject.toml")
    shutil.copy(CHECKOUT / "README.md", src / "README.md")
    if git:
        subprocess.run(["git", "init", "-q", str(src)], check=True)
        subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    # junk that must never travel
    (src / ".venv" / "lib").mkdir(parents=True)
    (src / ".venv" / "lib" / "big.so").write_bytes(b"\0" * 1000)
    (src / "claude_multi_usage" / "__pycache__").mkdir()
    (src / "claude_multi_usage" / "__pycache__" / "cli.pyc").write_bytes(b"\0")
    (src / "claude_multi_usage.egg-info").mkdir()
    (src / "claude_multi_usage.egg-info" / "PKG-INFO").write_text("x")
    (src / ".env").write_text("SECRET=1")
    (src / "server.db").write_bytes(b"\0" * 10)
    (src / ".claude").mkdir()
    (src / ".claude" / "settings.local.json").write_text("{}")
    (src / "data-2026-10-06.zip").write_bytes(b"PK")
    return src


def test_default_source_is_this_checkout():
    assert (CHECKOUT / "pyproject.toml").exists()
    assert remote.default_source() == str(CHECKOUT)


def test_default_source_falls_back_to_repository_url(monkeypatch, tmp_path):
    monkeypatch.setattr(remote, "PACKAGE_DIR", tmp_path / "site-packages" / "claude_multi_usage")

    class Meta:
        def get_all(self, key):
            return (
                ["Homepage, https://example.com/x", "Repository, https://github.com/me/cmu"]
                if key == "Project-URL"
                else None
            )

    monkeypatch.setattr(metadata, "metadata", lambda name: Meta())
    assert remote.default_source() == "git+https://github.com/me/cmu"

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "metadata", missing)
    with pytest.raises(RuntimeError, match="--from"):
        remote.default_source()


@pytest.mark.parametrize("git", [False, True])
def test_make_tarball_sends_only_source_files(tmp_path, git):
    if git and not shutil.which("git"):
        pytest.skip("git not available")
    src = _make_checkout(tmp_path, git=git)
    data = remote.make_tarball(src)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "src/pyproject.toml" in names
    assert "src/README.md" in names  # pyproject's readme=, which the build backend needs
    assert "src/claude_multi_usage/cli.py" in names
    for bad in (".git", ".venv", "__pycache__", "egg-info", ".env", "server.db", ".claude", ".zip"):
        assert not any(bad in n for n in names), (bad, names)
    assert len(data) < 2_000_000


def test_install_script_is_valid_bash_and_configures_everything():
    script = remote.install_script(
        True,
        None,
        "http://10.0.0.5:8000",
        [("k1", "team key"), ("k2", "")],
        alias="office box",
        every_minutes=7,
        python="3.12",
        linger=True,
    )
    assert _bash_syntax_ok(script)
    # uv is installed only if missing, into ~/.local/bin, without touching shell profiles
    assert 'curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN"' in script
    assert "UV_NO_MODIFY_PATH=1 sh" in script
    assert 'tool install --quiet --reinstall --python 3.12 "$BASE/src"' in script
    assert 'rm -rf "$BASE/src"' not in script  # kept, so `uv tool upgrade` keeps working there
    assert '"$CMU" config server http://10.0.0.5:8000' in script
    assert "config key add k1 'team key'" in script
    assert "config key add k2 ''" in script
    assert "config alias 'office box'" in script
    assert "autosync install --every 7 --linger" in script
    assert "autosync run --force" not in script  # install already performs the first sync
    assert 'UV_TOOL_BIN_DIR="$BASE/bin"' in script  # never clobber someone else's ~/.local/bin/cmu
    assert "pip install" not in script and "-m venv" not in script

    req = remote.install_script(
        False, "git+https://github.com/x/y.git", "http://s", [("k", "")], None, 15
    )
    assert _bash_syntax_ok(req)
    assert "tool install --quiet --reinstall git+https://github.com/x/y.git" in req
    assert "--python" not in req and "config alias" not in req and "--linger" not in req
    assert _bash_syntax_ok(remote.uninstall_script())
    assert "tool uninstall claude-multi-usage" in remote.uninstall_script()
    assert "autosync uninstall" in remote.uninstall_script()


def test_install_streams_checkout_then_runs_script(tmp_path):
    src = tmp_path / "co"
    (src / "claude_multi_usage").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    (src / "claude_multi_usage" / "__init__.py").write_text("")
    calls = []

    def fake_ssh(host, args, input_bytes, ssh_options):
        calls.append((host, args, input_bytes, ssh_options))

    lines = remote.install(
        "alice@box",
        "http://s:8000",
        [("k1", "")],
        15,
        alias="box",
        source=str(src),
        python="3.12",
        ssh_options=["-p", "2222"],
        linger=True,
        run=fake_ssh,
    )
    assert len(calls) == 2
    host, args, tarball, opts = calls[0]
    assert host == "alice@box" and opts == ["-p", "2222"]
    assert args[:2] == ["bash", "-c"] and "tar -xzf - -C ~/.local/share/cmu" in args[2]
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        assert "src/pyproject.toml" in tar.getnames()
    host, args, script, _ = calls[1]
    assert args == ["bash", "-s"]
    assert b'tool install --quiet --reinstall --python 3.12 "$BASE/src"' in script
    assert b"config alias box" in script and b"--linger" in script
    assert any("sent this checkout" in line for line in lines)


def test_install_from_requirement_sends_no_tarball():
    calls = []
    remote.install(
        "box",
        "http://s",
        [("k", "")],
        15,
        source="git+https://github.com/x/y.git",
        run=lambda *a: calls.append(a),
    )
    assert len(calls) == 1
    assert calls[0][1] == ["bash", "-s"]
    assert b"tool install --quiet --reinstall git+https://github.com/x/y.git" in calls[0][2]


def test_uninstall_runs_script():
    calls = []
    assert remote.uninstall("box", run=lambda *a: calls.append(a)) == ["cmu removed from box"]
    assert calls[0][1] == ["bash", "-s"] and b"autosync uninstall" in calls[0][2]

    def nothing_there(*a):
        raise subprocess.CalledProcessError(remote.NOTHING_TO_REMOVE, ["ssh"])

    assert remote.uninstall("box", run=nothing_there) == [
        "no cmu install found on box; nothing removed"
    ]

    def ssh_broken(*a):
        raise subprocess.CalledProcessError(255, ["ssh"])

    with pytest.raises(subprocess.CalledProcessError):
        remote.uninstall("box", run=ssh_broken)


# -- CLI --------------------------------------------------------------------------


def test_cli_remote_install_uses_local_config_by_default(fake_home, monkeypatch):
    config.set_server_url("http://sync.invalid")
    config.add_key("k1", "team")
    seen = {}

    def fake_install(
        host,
        server_url,
        keys,
        every,
        alias=None,
        source=None,
        python=None,
        ssh_options=None,
        linger=False,
    ):
        seen.update(
            host=host,
            server_url=server_url,
            keys=keys,
            every=every,
            alias=alias,
            source=source,
            python=python,
            ssh_options=ssh_options,
            linger=linger,
        )
        return ["done"]

    monkeypatch.setattr(remote, "install", fake_install)
    r = CliRunner().invoke(
        cli.main,
        [
            "remote",
            "install",
            "box",
            "--every",
            "5",
            "--alias",
            "lap",
            "--ssh-option",
            "-o StrictHostKeyChecking=accept-new",
            "--linger",
        ],
    )
    assert r.exit_code == 0, r.output
    assert seen == {
        "host": "box",
        "server_url": "http://sync.invalid",
        "keys": [("k1", "team")],
        "every": 5,
        "alias": "lap",
        "source": None,
        "python": None,
        "ssh_options": ["-o", "StrictHostKeyChecking=accept-new"],
        "linger": True,
    }
    assert "done" in r.output and "Warning" not in r.output


def test_cli_remote_install_overrides_and_localhost_warning(fake_home, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        remote,
        "install",
        lambda host, server_url, keys, every, **kw: (
            seen.update(server_url=server_url, keys=keys, **kw) or []
        ),
    )
    r = CliRunner().invoke(
        cli.main,
        [
            "remote",
            "install",
            "box",
            "--server",
            "http://localhost:8000",
            "--key",
            "a",
            "--key",
            "b",
            "--from",
            "git+https://g/r.git",
            "--python",
            "3.11",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "Warning" in r.output and "points at this machine" in r.output
    assert seen["server_url"] == "http://localhost:8000" and seen["keys"] == [("a", ""), ("b", "")]
    assert seen["source"] == "git+https://g/r.git" and seen["python"] == "3.11"


def test_cli_remote_install_requires_config(fake_home):
    r = CliRunner().invoke(cli.main, ["remote", "install", "box"])
    assert r.exit_code == 1 and "No server URL" in r.output
    config.set_server_url("http://x")
    r = CliRunner().invoke(cli.main, ["remote", "install", "box"])
    assert r.exit_code == 1 and "No keys" in r.output


@pytest.mark.parametrize(
    "code,text", [(255, "ssh to box failed"), (1, "Setup on box failed (exit 1)")]
)
def test_cli_remote_install_reports_failures(fake_home, monkeypatch, code, text):
    config.set_server_url("http://x")
    config.add_key("k")

    def failing(*a, **k):
        raise subprocess.CalledProcessError(code, ["ssh"])

    monkeypatch.setattr(remote, "install", failing)
    r = CliRunner().invoke(cli.main, ["remote", "install", "box"])
    assert r.exit_code == 1 and text in r.output


def test_cli_remote_uninstall(fake_home, monkeypatch):
    monkeypatch.setattr(
        remote, "uninstall", lambda host, ssh_options=None: [f"cmu removed from {host}"]
    )
    r = CliRunner().invoke(cli.main, ["remote", "uninstall", "box"])
    assert r.exit_code == 0 and "cmu removed from box" in r.output


@pytest.mark.parametrize(
    "code,text", [(255, "ssh to box failed"), (1, "Uninstall on box failed (exit 1)")]
)
def test_cli_remote_uninstall_reports_failures(fake_home, monkeypatch, code, text):
    def failing(*a, **k):
        raise subprocess.CalledProcessError(code, ["ssh"])

    monkeypatch.setattr(remote, "uninstall", failing)
    r = CliRunner().invoke(cli.main, ["remote", "uninstall", "box"])
    assert r.exit_code == 1 and text in r.output


# -- the scripts, run for real against a fake uv -----------------------------------


def test_tarball_builds_with_uv(tmp_path):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not available")
    with tarfile.open(fileobj=io.BytesIO(remote.make_tarball(CHECKOUT)), mode="r:gz") as tar:
        tar.extractall(tmp_path, **({"filter": "data"} if sys.version_info >= (3, 12) else {}))
    res = subprocess.run(
        [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(tmp_path / "dist"),
            str(tmp_path / "src"),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 and "offline" in res.stderr.lower():
        pytest.skip("uv would need the network to build here")
    assert res.returncode == 0, res.stderr
    assert list((tmp_path / "dist").glob("claude_multi_usage-*.whl"))


FAKE_UV = """#!/bin/sh
# Stand-in for uv: logs every call and emulates `tool dir`, `tool install`, `tool uninstall`.
echo "uv[@@TAG@@] $* [UV_TOOL_BIN_DIR=${UV_TOOL_BIN_DIR:-}]" >> "$HOME/uv.log"
TOOLS="$HOME/.local/share/uv/tools"
BINDIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
case "$*" in
    --version) echo "uv 9.9.9 (@@TAG@@)" ;;
    "tool dir") echo "$TOOLS" ;;
    "tool dir --bin") echo "$BINDIR" ;;
    "tool install"*)
        if [ -n "${FAKE_UV_FAIL:-}" ]; then echo "fake uv: install failed" >&2; exit 1; fi
        mkdir -p "$TOOLS/claude-multi-usage/bin" "$BINDIR"
        for exe in cmu claude-multi-usage; do
            target="$TOOLS/claude-multi-usage/bin/$exe"
            if [ -e "$BINDIR/$exe" ] \\
                    && [ "$(readlink "$BINDIR/$exe" 2>/dev/null)" != "$target" ]; then
                echo "error: Executable already exists: $exe (use --force to overwrite)" >&2
                exit 2
            fi
        done
        for exe in cmu claude-multi-usage; do
            target="$TOOLS/claude-multi-usage/bin/$exe"
            printf '%s\\n' '#!/bin/sh' 'echo "cmu $*" >> "$HOME/cmu.log"' \\
                '[ "$1" = --version ] && echo "cmu, version 0.0.0"' 'exit 0' > "$target"
            chmod +x "$target"
            ln -sfn "$target" "$BINDIR/$exe"
        done ;;
    "tool uninstall"*)
        rm -rf "${TOOLS:?}/claude-multi-usage"
        for exe in cmu claude-multi-usage; do rm -f "${BINDIR:?}/${exe:?}"; done ;;
    *) echo "fake uv: unexpected call: $*" >&2; exit 99 ;;
esac
"""


def _install():
    return remote.install_script(
        True, None, "http://10.0.0.5:8000", [("k1", "team")], "box", 7, linger=True
    )


@pytest.fixture
def fake_remote(tmp_path, monkeypatch):
    """A throwaway HOME, a PATH holding only the tools the scripts need, and helpers.

    The scripts also look for uv in /opt/homebrew/bin and /usr/local/bin; those are
    pointed at nothing so a real uv on the test machine stays out of the picture.
    """
    monkeypatch.setattr(remote, "UV_FALLBACKS", ("$HOME/.local/bin/uv", f"{tmp_path}/none/uv"))
    needed = (
        "bash",
        "hostname",
        "readlink",
        "mkdir",
        "cut",
        "rm",
        "ln",
        "cat",
        "chmod",
        "sh",
        "env",
    )
    missing = [t for t in needed if not shutil.which(t)]
    if missing:
        pytest.skip(f"not available: {missing}")
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    for tool in needed:
        (path_dir / tool).symlink_to(shutil.which(tool))

    def make_uv(where, tag="fake"):
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(FAKE_UV.replace("@@TAG@@", tag))
        where.chmod(0o755)
        return where

    def run(script, extra_path=(), **env_extra):
        env = {
            "HOME": str(home),
            "USER": "tester",
            "PATH": os.pathsep.join([*(str(p) for p in extra_path), str(path_dir)]),
            **env_extra,
        }
        return subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, env=env)

    def log(name):
        f = home / name
        return f.read_text() if f.exists() else ""

    return types.SimpleNamespace(
        home=home,
        bin=home / ".local" / "bin",
        base=home / ".local" / "share" / "cmu",
        tool_cmu=home / ".local/share/uv/tools/claude-multi-usage/bin/cmu",
        make_uv=make_uv,
        run=run,
        log=log,
    )


def test_script_prefers_the_uv_on_path(fake_remote):
    fake_remote.make_uv(fake_remote.bin / "uv", tag="local")
    on_path = fake_remote.make_uv(fake_remote.home / "elsewhere" / "uv", tag="path")
    res = fake_remote.run(_install(), extra_path=[on_path.parent])
    assert res.returncode == 0, res.stderr
    assert "uv[path] tool install" in fake_remote.log("uv.log")
    assert "uv[local]" not in fake_remote.log("uv.log")


@pytest.mark.parametrize("downloader", ["curl", "wget"])
def test_script_installs_uv_only_when_missing(fake_remote, downloader):
    # The fake downloader prints an "installer" that drops a fake uv where UV_INSTALL_DIR says.
    (fake_remote.home / "installer.sh").write_text(
        'echo "UV_INSTALL_DIR=$UV_INSTALL_DIR UV_NO_MODIFY_PATH=$UV_NO_MODIFY_PATH"'
        ' >> "$HOME/installer.log"\n'
        'mkdir -p "$UV_INSTALL_DIR"\n'
        'cat > "$UV_INSTALL_DIR/uv" <<"UVEOF"\n'
        + FAKE_UV.replace("@@TAG@@", "downloaded")
        + "UVEOF\n"
        'chmod +x "$UV_INSTALL_DIR/uv"\n'
    )
    dl = fake_remote.home / "dl"
    dl.mkdir()
    (dl / downloader).write_text('#!/bin/sh\ncat "$HOME/installer.sh"\n')
    (dl / downloader).chmod(0o755)
    res = fake_remote.run(_install(), extra_path=[dl])
    assert res.returncode == 0, res.stderr
    assert "installing uv into" in res.stdout
    assert fake_remote.log("installer.log").strip() == (
        f"UV_INSTALL_DIR={fake_remote.bin} UV_NO_MODIFY_PATH=1"
    )
    assert "uv[downloaded] tool install" in fake_remote.log("uv.log")
    # second run: uv is there now, so the downloader must not be used again
    (fake_remote.home / "installer.log").unlink()
    res = fake_remote.run(_install(), extra_path=[dl])
    assert res.returncode == 0, res.stderr
    assert "installing uv into" not in res.stdout and fake_remote.log("installer.log") == ""


def test_script_fails_clearly_without_curl_or_wget(fake_remote):
    res = fake_remote.run(_install())
    assert res.returncode == 1
    assert "curl or wget is needed" in res.stderr


@pytest.mark.parametrize("foreign", ["cmu", "claude-multi-usage"])
def test_script_never_clobbers_a_foreign_command(fake_remote, foreign):
    fake_remote.make_uv(fake_remote.bin / "uv")
    (fake_remote.bin / foreign).write_text("#!/bin/sh\necho foreign\n")
    (fake_remote.bin / foreign).chmod(0o755)
    res = fake_remote.run(_install())
    assert res.returncode == 0, res.stderr
    assert f"{foreign} exists and is not ours" in res.stdout
    assert (fake_remote.bin / foreign).read_text() == "#!/bin/sh\necho foreign\n"
    assert os.readlink(fake_remote.base / "bin" / "cmu") == str(fake_remote.tool_cmu)
    assert f"UV_TOOL_BIN_DIR={fake_remote.base / 'bin'}" in fake_remote.log("uv.log")


def test_script_replaces_a_venv_era_install(fake_remote):
    fake_remote.make_uv(fake_remote.bin / "uv")
    old = fake_remote.base / "venv" / "bin" / "cmu"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\n")
    old.chmod(0o755)
    (fake_remote.bin / "cmu").symlink_to(old)
    res = fake_remote.run(_install())
    assert res.returncode == 0, res.stderr
    assert "not ours" not in res.stdout
    assert not (fake_remote.base / "venv").exists()
    assert os.readlink(fake_remote.bin / "cmu") == str(fake_remote.tool_cmu)


def test_script_keeps_a_venv_era_install_when_the_new_install_fails(fake_remote):
    fake_remote.make_uv(fake_remote.bin / "uv")
    old = fake_remote.base / "venv" / "bin" / "cmu"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\n")
    old.chmod(0o755)
    (fake_remote.bin / "cmu").symlink_to(old)
    res = fake_remote.run(_install(), FAKE_UV_FAIL="1")
    assert res.returncode != 0 and "install failed" in res.stderr
    assert old.exists()  # the old install still works...
    assert os.readlink(fake_remote.bin / "cmu") == str(old)  # ...and is still on PATH


def test_script_configures_then_schedules(fake_remote):
    fake_remote.make_uv(fake_remote.bin / "uv")
    res = fake_remote.run(_install())
    assert res.returncode == 0, res.stderr
    assert fake_remote.log("cmu.log").splitlines() == [
        "cmu config server http://10.0.0.5:8000",
        "cmu config key add k1 team",
        "cmu config alias box",
        "cmu autosync install --every 7 --linger",
        "cmu autosync status",
    ]
    assert f"tool install --quiet --reinstall {fake_remote.base / 'src'}" in fake_remote.log(
        "uv.log"
    )


def test_uninstall_script_removes_a_uv_tool_install(fake_remote):
    fake_remote.make_uv(fake_remote.bin / "uv")
    assert fake_remote.run(_install()).returncode == 0
    res = fake_remote.run(remote.uninstall_script())
    assert res.returncode == 0, res.stderr
    assert "removed cmu (uv tool)" in res.stdout and "~/.claude-multi-usage" in res.stdout
    assert "cmu autosync uninstall" in fake_remote.log("cmu.log")
    assert "tool uninstall claude-multi-usage" in fake_remote.log("uv.log")
    assert not fake_remote.base.exists() and not (fake_remote.bin / "cmu").exists()


def test_uninstall_script_removes_a_venv_era_install(fake_remote):
    old = fake_remote.base / "venv" / "bin" / "cmu"
    old.parent.mkdir(parents=True)
    old.write_text('#!/bin/sh\necho "old-cmu $*" >> "$HOME/cmu.log"\n')
    old.chmod(0o755)
    (fake_remote.bin / "cmu").symlink_to(old)
    res = fake_remote.run(remote.uninstall_script())
    assert res.returncode == 0, res.stderr
    assert "removed cmu (venv)" in res.stdout
    assert "old-cmu autosync uninstall" in fake_remote.log("cmu.log")
    assert not fake_remote.base.exists()
    assert not (fake_remote.bin / "cmu").is_symlink() and not (fake_remote.bin / "cmu").exists()


def test_uninstall_script_reports_a_job_it_could_not_remove(fake_remote):
    old = fake_remote.base / "venv" / "bin" / "cmu"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\nexit 1\n")  # e.g. the venv's interpreter is gone
    old.chmod(0o755)
    res = fake_remote.run(remote.uninstall_script())
    assert res.returncode == 0, res.stderr
    assert "autosync job could not be removed" in res.stderr
    assert "systemctl --user disable --now cmu-autosync.timer" in res.stderr
    assert "launchctl bootout" in res.stderr
    assert "and its autosync job" not in res.stdout


def test_uninstall_script_warns_when_nothing_is_installed(fake_remote):
    res = fake_remote.run(remote.uninstall_script())
    assert res.returncode == remote.NOTHING_TO_REMOVE
    assert "no cmu install found" in res.stderr
