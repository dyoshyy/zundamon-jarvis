"""install.sh against stub binaries on PATH: nothing real is installed or downloaded."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is the POSIX installer (Windows has install.ps1)")

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
TOOLS = ("sed", "awk", "head", "mkdir", "cat", "cp", "basename", "dirname", "rm", "env", "sh")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


class Env:
    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.sys = tmp / "sysbin"  # only the plain tools: no real claude/uv/curl leaks in
        self.sys.mkdir()
        for t in TOOLS:
            found = shutil.which(t)
            if found:
                (self.sys / t).symlink_to(found)
        self.log = tmp / "calls.log"
        self.ver = tmp / "installed-version"
        self.home.mkdir()
        self.bin.mkdir()
        self.log.touch()
        self.fail = tmp / "fail-on"  # first line: a command-line prefix that should fail
        log = f'echo "$(basename "$0") $*" >> "{self.log}"\n'
        failcheck = (f'if [ -f "{self.fail}" ] && case "$*" in "$(cat "{self.fail}")"*) true;; *) false;; esac;'
                     ' then exit 1; fi\n')
        _stub(self.bin, "uname", 'case "$1" in -s) echo Darwin;; -m) echo arm64;; esac\n')
        _stub(self.bin, "sw_vers", "echo 14.5\n")
        _stub(self.bin, "claude", "exit 0\n")
        _stub(self.bin, "git", "exit 0\n")
        _stub(self.bin, "df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
                              'echo "/dev/x 999 1 99999999 1% /"\n')
        _stub(self.bin, "curl", log + 'case "$*" in\n'
              '  *api.github.com*) echo \'{"tag_name": "v0.1.0", "name": "x"}\';;\n'
              '  *astral.sh*) echo "exit 0";;\nesac\n')
        _stub(self.bin, "uv", log + failcheck +
              'case "$1 $2" in\n'
              f'  "tool dir") echo "{self.home}/.local/bin";;\n'
              f'  "tool install") for a in "$@"; do case "$a" in git+*) echo "${{a##*@}}" | sed "s/^v//" > "{self.ver}";; esac; done;'
              f' cp "{self.bin}/zj-real" "{self.bin}/zundamon-jarvis";;\nesac\n')
        _stub(self.bin, "zj-real", f'''if [ "$1" = "--version" ]; then cat "{self.ver}"; exit 0; fi
echo "zundamon-jarvis $*" >> "{self.log}"
if [ -f "{self.fail}" ] && case "$*" in "$(cat "{self.fail}")"*) true;; *) false;; esac; then exit 1; fi
''')

    def run(self, **extra) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:{self.sys}", "HOME": str(self.home), "ZJ_TTY": "/dev/null", **extra}
        return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_second_run_skips_tool_install_and_reruns_setup(env):
    first = env.run()
    assert first.returncode == 0, first.stderr
    calls = env.calls()
    assert any(c.startswith("uv tool install --force --python 3.12 git+https://github.com/dyoshyy/zundamon-jarvis@v0.1.0") for c in calls)
    # the setup page fetches the engine and the model itself, with progress
    zj = [c for c in calls if c.startswith("zundamon-jarvis")]
    assert zj == ["zundamon-jarvis setup"]
    assert "==> " in first.stdout and "ブラウザ" in first.stdout

    env.log.write_text("")
    second = env.run()
    assert second.returncode == 0, second.stderr
    again = env.calls()
    assert not any(c.startswith("uv tool install") for c in again)
    assert "zundamon-jarvis setup" in again  # idempotent: shows the current values
    assert not (env.home / ".local" / "state").exists()


def test_cli_mode_fetches_in_the_terminal_and_runs_the_terminal_wizard(env):
    r = env.run(ZJ_CLI="1", ZJ_TTY=os.devnull)
    assert r.returncode == 0, r.stderr
    zj = [c for c in env.calls() if c.startswith("zundamon-jarvis")]
    assert zj == ["zundamon-jarvis engine install", "zundamon-jarvis models download",
                  "zundamon-jarvis setup --cli"]


def test_install_forces_reinstall_when_version_differs(env):
    env.run(ZJ_VERSION="v0.1.0")
    env.log.write_text("")
    r = env.run(ZJ_VERSION="v0.2.0")
    assert r.returncode == 0, r.stderr
    assert any(c.startswith("uv tool install --force --python 3.12") and c.endswith("@v0.2.0") for c in env.calls())


def test_install_refuses_intel_mac(env):
    _stub(env.bin, "uname", 'case "$1" in -s) echo Darwin;; -m) echo x86_64;; esac\n')
    r = env.run()
    assert r.returncode != 0
    assert "Apple Silicon" in r.stdout + r.stderr
    assert not any(c.startswith("uv ") for c in env.calls())


def test_install_refuses_old_macos(env):
    _stub(env.bin, "sw_vers", "echo 13.6\n")
    r = env.run()
    assert r.returncode != 0
    assert "macOS 14" in r.stdout + r.stderr
    assert env.calls() == []


def test_install_points_linux_to_readme(env):
    _stub(env.bin, "uname", 'case "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n')
    r = env.run()
    assert r.returncode != 0
    assert "README" in r.stdout + r.stderr
    assert "https://github.com/dyoshyy/zundamon-jarvis#linux" in r.stdout + r.stderr


def test_install_stops_without_claude(env):
    (env.bin / "claude").unlink()
    r = env.run()
    assert r.returncode != 0
    assert "claude" in r.stdout + r.stderr
    assert not any(c.startswith("uv ") for c in env.calls())


def test_install_stops_when_disk_below_8gb(env):
    _stub(env.bin, "df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
                         'echo "/dev/x 999 1 1000000 99% /"\n')
    r = env.run()
    assert r.returncode != 0
    assert "8 GB" in r.stdout + r.stderr


def test_install_reports_failed_step(env):
    env.fail.write_text("models download")
    r = env.run(ZJ_CLI="1")
    assert r.returncode != 0
    assert "モデル" in r.stderr  # the failed step's name
    assert "もう一度" in r.stderr
    assert "zundamon-jarvis setup --cli" not in env.calls()
    # a rerun just runs the idempotent steps again and now gets through
    env.fail.unlink()
    env.log.write_text("")
    r2 = env.run(ZJ_CLI="1")
    assert r2.returncode == 0, r2.stderr
    again = env.calls()
    assert "zundamon-jarvis models download" in again
    assert "zundamon-jarvis setup --cli" in again


def test_unfinished_setup_page_says_how_to_continue(env):
    env.fail.write_text("setup")
    r = env.run()
    assert r.returncode != 0
    assert "zundamon-jarvis setup を実行すると、続きから" in r.stderr
    assert "完了しました" not in r.stdout


def test_install_uses_zj_version_without_asking_github(env):
    r = env.run(ZJ_VERSION="v9.9.9")
    assert r.returncode == 0, r.stderr
    assert any(c.endswith("zundamon-jarvis@v9.9.9") for c in env.calls())
    assert not any("api.github.com" in c for c in env.calls())


def test_install_explains_when_latest_release_unavailable(env):
    _stub(env.bin, "curl", "exit 22\n")
    r = env.run()
    assert r.returncode != 0
    assert "ZJ_VERSION=v0.1.0" in r.stdout + r.stderr


def test_install_fails_loudly_when_uv_installer_leaves_no_uv(env):
    (env.bin / "uv").unlink()
    r = env.run()
    assert r.returncode != 0  # the stub installer installs nothing, so the step must fail loudly
    assert "uv" in r.stderr


def test_cli_setup_without_tty_tells_user(env):
    r = env.run(ZJ_CLI="1", ZJ_TTY="/nonexistent/tty")
    assert r.returncode == 0
    assert "zundamon-jarvis setup --cli" in r.stdout
    assert "zundamon-jarvis setup --cli" not in env.calls()


def test_setup_page_needs_no_tty(env):
    r = env.run(ZJ_TTY="/nonexistent/tty")
    assert r.returncode == 0, r.stderr
    assert "zundamon-jarvis setup" in env.calls()


def test_dry_run_prints_without_running(env):
    r = env.run(ZJ_DRY_RUN="1", ZJ_VERSION="v0.1.0")
    assert r.returncode == 0, r.stderr
    assert "uv tool install" in r.stdout and "[dry-run] zundamon-jarvis setup" in r.stdout
    assert "engine install" not in r.stdout
    r = env.run(ZJ_DRY_RUN="1", ZJ_VERSION="v0.1.0", ZJ_CLI="1")
    assert "engine install" in r.stdout and "setup --cli" in r.stdout
    assert not any(c.startswith(("uv tool install", "zundamon-jarvis")) for c in env.calls())


def test_install_sh_passes_shellcheck():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_install_stops_without_git(env):
    (env.bin / "git").unlink()
    r = env.run()
    assert r.returncode != 0
    assert "xcode-select --install" in r.stdout + r.stderr
    assert not any(c.startswith("uv ") for c in env.calls())


def test_install_stops_when_git_is_only_the_clt_shim(env):
    _stub(env.bin, "git", "exit 1\n")
    r = env.run()
    assert r.returncode != 0
    assert "xcode-select --install" in r.stdout + r.stderr


def test_install_finds_claude_in_local_bin(env):
    (env.bin / "claude").unlink()
    (env.home / ".local" / "bin").mkdir(parents=True)
    _stub(env.home / ".local" / "bin", "claude", "exit 0\n")
    r = env.run()
    assert r.returncode == 0, r.stderr


def test_install_explains_rosetta(env):
    _stub(env.bin, "uname", 'case "$1" in -s) echo Darwin;; -m) echo x86_64;; esac\n')
    _stub(env.bin, "sysctl", "echo 1\n")
    r = env.run()
    assert r.returncode != 0
    assert "Rosetta" in r.stdout + r.stderr


def test_children_do_not_read_the_script_from_stdin(env):
    # `curl | sh`: stdin is the script itself; a child that reads stdin would swallow the rest of it
    _stub(env.bin, "zj-real", f'''if [ "$1" = "--version" ]; then cat "{env.ver}"; exit 0; fi
echo "zundamon-jarvis $*" >> "{env.log}"
cat > /dev/null
''')
    env_vars = {"PATH": f"{env.bin}:{env.sys}", "HOME": str(env.home), "ZJ_TTY": "/dev/null"}
    r = subprocess.run(f'cat "{SCRIPT}" | sh', shell=True, env=env_vars, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "==> 完了しました" in r.stdout


def test_truncated_script_runs_nothing(env, tmp_path):
    cut = tmp_path / "cut.sh"
    cut.write_text(SCRIPT.read_text().rsplit("main", 1)[0][:-200])
    r = subprocess.run(["sh", str(cut)], env={"PATH": f"{env.bin}:{env.sys}", "HOME": str(env.home)},
                       capture_output=True, text=True)
    assert env.calls() == []
