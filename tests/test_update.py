import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from zundamon_jarvis import cli, update
from zundamon_jarvis.config import LANGUAGES

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="runs the /bin/sh helper")


# ---- versions ----

def test_parse_version():
    assert update.parse_version("v0.1.0") == (0, 1, 0)
    assert update.parse_version("0.10.2") == (0, 10, 2)
    assert update.parse_version(" V1.2 ") == (1, 2)
    assert update.parse_version("v0.2.0rc1") == (0, 2, 0)
    assert update.parse_version("latest") is None
    assert update.parse_version(None) is None


def test_is_newer():
    assert update.is_newer("v0.2.0", "0.1.0")
    assert update.is_newer("v0.10.0", "0.9.9")  # numbers, not strings
    assert update.is_newer("v1.0.1", "1.0")
    assert not update.is_newer("v0.1.0", "0.1.0")
    assert not update.is_newer("v1.0", "1.0.0")
    assert not update.is_newer("v0.1.0", "0.2.0")
    assert not update.is_newer(None, "0.1.0")
    assert not update.is_newer("v0.2.0", None)
    assert not update.is_newer("nightly", "0.1.0")


def test_installed_version():
    assert update.installed_version(lambda name: "1.2.3") == "1.2.3"

    def missing(name):
        raise update.importlib.metadata.PackageNotFoundError(name)

    assert update.installed_version(missing) is None


# ---- the release request ----

class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_latest_release_reads_the_tag_and_sends_a_user_agent():
    seen = {}

    def urlopen(req, timeout):
        seen["ua"], seen["url"], seen["timeout"] = req.get_header("User-agent"), req.full_url, timeout
        return Resp(json.dumps({"tag_name": "v0.2.0", "name": "x"}).encode())

    assert update.latest_release(urlopen, user_agent="zj-test") == "v0.2.0"
    assert seen == {"ua": "zj-test", "url": update.LATEST_URL, "timeout": update.FETCH_TIMEOUT_S}


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError(update.LATEST_URL, 404, "Not Found", {}, None),  # private repo / no release
    urllib.error.HTTPError(update.LATEST_URL, 403, "rate limited", {}, None),
    urllib.error.URLError("no network"),
    TimeoutError("slow"),
])
def test_latest_release_is_quiet_about_failures(failure):
    def urlopen(req, timeout):
        raise failure

    assert update.latest_release(urlopen, user_agent="t") is None


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"name": "x"}', b'{"tag_name": ""}', b'{"tag_name": 3}'])
def test_latest_release_without_a_tag(body):
    assert update.latest_release(lambda req, timeout: Resp(body), user_agent="t") is None


# ---- the cache ----

def test_check_asks_once_then_uses_the_cache(tmp_path):
    path, calls = tmp_path / "d" / "update.json", []

    def fetch():
        calls.append(1)
        return "v0.2.0"

    first = update.check(path, 1000.0, fetch)
    assert first == update.Known("v0.2.0", 1000.0)
    assert json.loads(path.read_text(encoding="utf-8")) == {"latest": "v0.2.0", "checked": 1000.0}
    assert update.check(path, 1000.0 + update.MAX_AGE_S - 1, fetch) == first
    assert len(calls) == 1
    update.check(path, 1000.0 + update.MAX_AGE_S, fetch)
    assert len(calls) == 2


def test_check_forced_or_from_the_future_asks_again(tmp_path):
    path = tmp_path / "update.json"
    update.write_cache(path, update.Known("v0.1.0", 5000.0))
    assert update.check(path, 5001.0, lambda: "v0.2.0", force=True).latest == "v0.2.0"
    update.write_cache(path, update.Known("v0.1.0", 5000.0))
    assert update.check(path, 10.0, lambda: "v0.3.0").latest == "v0.3.0"  # the clock went back


def test_check_failure_keeps_the_old_answer_and_asks_again_next_time(tmp_path):
    path = tmp_path / "update.json"
    update.write_cache(path, update.Known("v0.2.0", 100.0))
    got = update.check(path, 100.0 + update.MAX_AGE_S, lambda: None)
    assert got == update.Known("v0.2.0", 100.0, failed=True)
    assert update.read_cache(path) == update.Known("v0.2.0", 100.0)  # not rewritten
    assert update.check(tmp_path / "none.json", 1.0, lambda: None) == update.Known(None, None, failed=True)


@pytest.mark.parametrize("content", ["", "not json", "[1]", '{"latest": 3, "checked": "x"}'])
def test_a_broken_cache_is_no_answer(tmp_path, content):
    path = tmp_path / "update.json"
    path.write_text(content, encoding="utf-8")
    assert update.read_cache(path) == update.Known()


# ---- how it was installed ----

def _checkout(root: Path) -> Path:
    (root / "src" / "zundamon_jarvis").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return root / "src" / "zundamon_jarvis"


def _no_tools():
    return None


def test_detect_git_checkout(tmp_path):
    pkg = _checkout(tmp_path / "repo")
    venv = tmp_path / "repo" / ".venv"
    got = update.detect_install(prefix=venv, package_dir=pkg, uv_tool_dir=_no_tools)
    assert got == update.Install("git", tmp_path / "repo")


def test_detect_worktree_checkout(tmp_path):
    pkg = _checkout(tmp_path / "wt")
    (tmp_path / "wt" / ".git").rmdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")  # a worktree's .git is a file
    assert update.detect_install(prefix=tmp_path, package_dir=pkg, uv_tool_dir=_no_tools).kind == "git"


def test_detect_uv_tool_by_its_receipt(tmp_path):
    env = tmp_path / "tools" / "zundamon-jarvis"
    pkg = env / "lib" / "python3.12" / "site-packages" / "zundamon_jarvis"
    pkg.mkdir(parents=True)
    (env / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    assert update.detect_install(prefix=env, package_dir=pkg, uv_tool_dir=_no_tools) == update.Install("uv-tool", env)


def test_detect_uv_tool_by_uv_tool_dir(tmp_path):
    env = tmp_path / "tools" / "zundamon-jarvis"
    pkg = env / "Lib" / "site-packages" / "zundamon_jarvis"
    pkg.mkdir(parents=True)
    got = update.detect_install(prefix=env, package_dir=pkg, uv_tool_dir=lambda: tmp_path / "tools")
    assert got.kind == "uv-tool"
    other = update.detect_install(prefix=tmp_path / "venv", package_dir=pkg, uv_tool_dir=lambda: tmp_path / "tools")
    assert other == update.Install("unknown", None)


def test_detect_editable_tool_install_of_a_checkout(tmp_path):
    pkg = _checkout(tmp_path / "repo")
    env = tmp_path / "tools" / "zundamon-jarvis"
    env.mkdir(parents=True)
    (env / "uv-receipt.toml").write_text("", encoding="utf-8")
    assert update.detect_install(prefix=env, package_dir=pkg, uv_tool_dir=_no_tools).kind == "editable-tool"


def test_find_uv(tmp_path):
    def files(*paths):
        known = {os.path.normpath(p) for p in paths}
        return lambda p: os.path.normpath(str(p)) in known

    home = tmp_path / "home"
    local = home / ".local" / "bin"
    assert update.find_uv({"UV": str(tmp_path / "uvx")}, lambda n: None, home, "linux",
                          files(tmp_path / "uvx")) == os.path.normpath(str(tmp_path / "uvx"))
    assert update.find_uv({}, lambda n: str(tmp_path / "onpath"), home, "linux",
                          files(tmp_path / "onpath")) == os.path.normpath(str(tmp_path / "onpath"))
    assert update.find_uv({"UV": str(tmp_path / "gone")}, lambda n: None, home, "linux",
                          files(local / "uv")) == os.path.normpath(str(local / "uv"))
    assert update.find_uv({}, lambda n: None, home, "windows",
                          files(local / "uv.exe")) == os.path.normpath(str(local / "uv.exe"))
    assert update.find_uv({}, lambda n: None, home, "linux", files()) is None


# ---- the helper scripts ----

def test_source_is_the_tag_zip_like_install_ps1():
    assert update.source("v0.2.0") == ("zundamon-jarvis @ https://github.com/dyoshyy/zundamon-jarvis"
                                       "/archive/refs/tags/v0.2.0.zip")
    ps1 = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")
    assert "zundamon-jarvis @ https://github.com/$OwnerRepo/archive/refs/tags/$Version.zip" in ps1


def _order(text, *needles):
    at = [text.index(n) for n in needles]
    assert at == sorted(at), needles


def test_windows_script_stops_installs_and_starts_again():
    s = update.windows_script(r"C:\Users\o'neil\.local\bin\uv.exe", r"C:\Users\o'neil\zj.exe", "v0.2.0",
                              Path(r"C:\data\update.running"))
    assert r"$uv = 'C:\Users\o''neil\.local\bin\uv.exe'" in s  # PowerShell's quote doubling
    assert "$source = 'zundamon-jarvis @ https://github.com/dyoshyy/zundamon-jarvis/archive/refs/tags/v0.2.0.zip'" in s
    _order(s, "& $zj service stop", "& $uv tool install --force --python 3.12 $source", "& $zj service start",
           "Remove-Item -LiteralPath $marker")
    # the service is started again whether the install worked or not: not inside an if
    assert any(line == "& $zj service start 2>&1 | Out-String" for line in s.splitlines())
    assert s.isascii()  # PowerShell 5.1 reads it in the ANSI code page if the BOM were lost


def test_mac_script_takes_the_agents_out_and_puts_them_back():
    s = update.posix_script("mac", "/Users/me/.local/bin/uv", "/Users/me/zj", "v0.2.0", Path("/d/update.running"), 501)
    _order(s, "launchctl bootout gui/501/dev.zundamon-jarvis.menubar", "launchctl bootout gui/501/dev.zundamon-jarvis.daemon",
           '"$UV" tool install --force --python 3.12 "$SOURCE"', "'/Users/me/zj' service install", 'rm -f "$MARKER"')
    assert "voicevox" not in s  # the engine is not in the tool environment


def test_linux_script_restarts_the_unit_if_it_runs():
    s = update.posix_script("linux", "/u/uv", "/u/zj", "v0.2.0", Path("/d/update.running"), 1000)
    assert "systemctl --user try-restart zundamon-jarvis.service" in s
    assert "launchctl" not in s


@POSIX_ONLY
@pytest.mark.parametrize("sys_", ["mac", "linux"])
def test_posix_script_is_valid_sh(tmp_path, sys_):
    path = tmp_path / "update.sh"
    path.write_text(update.posix_script(sys_, "/u/it's uv", "/u/zj", "v0.2.0", tmp_path / "m", 1), encoding="utf-8")
    assert subprocess.run(["sh", "-n", str(path)], capture_output=True).returncode == 0


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@POSIX_ONLY
@pytest.mark.parametrize("uv_ok", [True, False])
def test_mac_script_run_against_stubs(tmp_path, uv_ok):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    calls = tmp_path / "calls"
    rec = f'echo "$(basename "$0") $*" >> "{calls}"\n'
    _stub(bin_ / "launchctl", rec)
    _stub(bin_ / "sleep", "exit 0\n")
    uv = _stub(bin_ / "uv", rec + ("exit 0\n" if uv_ok else "exit 1\n"))
    zj = _stub(bin_ / "zj", rec)
    marker = tmp_path / "update.running"
    marker.write_text("x", encoding="utf-8")
    script = tmp_path / "update.sh"
    script.write_text(update.posix_script("mac", str(uv), str(zj), "v0.2.0", marker, 501), encoding="utf-8")
    env = {"PATH": f"{bin_}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path)}
    r = subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=30)
    lines = calls.read_text(encoding="utf-8").splitlines()
    installs = [line for line in lines if line.startswith("uv tool install")]
    assert len(installs) == (1 if uv_ok else update.INSTALL_TRIES)
    assert installs[0].endswith("zundamon-jarvis @ https://github.com/dyoshyy/zundamon-jarvis/archive/refs/tags/v0.2.0.zip")
    assert lines[:2] == ["launchctl bootout gui/501/dev.zundamon-jarvis.menubar",
                         "launchctl bootout gui/501/dev.zundamon-jarvis.daemon"]
    assert lines[-1] == "zj service install"  # also after a failed install
    assert r.returncode == (0 if uv_ok else 1)
    assert not marker.exists()


def test_helper_per_os(tmp_path):
    w = update.helper("windows", "uv.exe", "zj.exe", "v0.2.0", tmp_path)
    assert w.path == tmp_path / "update.ps1" and w.encoding == "utf-8-sig"
    assert w.argv[:1] == ["powershell.exe"] and w.argv[-2:] == ["-File", str(tmp_path / "update.ps1")]
    assert "-NoProfile" in w.argv and w.argv[w.argv.index("-WindowStyle") + 1] == "Hidden"
    m = update.helper("mac", "uv", "zj", "v0.2.0", tmp_path, uid=501)
    assert m.argv == ["/bin/sh", str(tmp_path / "update.sh")] and "gui/501/" in m.script


def test_start_helper_writes_the_script_marker_and_log_then_spawns(tmp_path):
    h = update.helper("windows", "uv.exe", r"C:\Users\ゆう\zj.exe", "v0.2.0", tmp_path / "data")
    spawned = []
    log = tmp_path / "logs" / "update.log"
    update.start_helper(h, "windows", log, 1000.0, "v0.2.0", spawn=lambda argv, s, lp: spawned.append((argv, s, lp)))
    assert spawned == [(h.argv, "windows", log)]
    raw = h.path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "ゆう".encode("utf-8") in raw  # PowerShell 5.1 needs the BOM
    assert (tmp_path / "data" / update.RUNNING).exists()
    assert "update to v0.2.0" in log.read_text(encoding="utf-8")


def test_start_helper_that_cannot_spawn_leaves_no_marker(tmp_path):
    h = update.helper("linux", "uv", "zj", "v0.2.0", tmp_path)

    def spawn(*a):
        raise OSError("no sh")

    with pytest.raises(OSError):
        update.start_helper(h, "linux", tmp_path / "update.log", 1.0, "v0.2.0", spawn=spawn)
    assert not (tmp_path / update.RUNNING).exists()


def test_in_progress(tmp_path):
    marker = tmp_path / update.RUNNING
    assert not update.in_progress(marker, 0.0)
    marker.write_text("", encoding="utf-8")
    os.utime(marker, (10_000, 10_000))
    assert update.in_progress(marker, 10_000 + 60)
    assert not update.in_progress(marker, 10_000 + update.RUNNING_STALE_S + 1)  # a helper that died


# ---- git ----

class FakeGit:
    def __init__(self, branch="main", upstream="origin/main", changes="", behind="2", fetch_ok=True):
        self.branch, self.upstream, self.changes, self.behind, self.fetch_ok = branch, upstream, changes, behind, fetch_ok
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(argv[1:])
        sub = argv[1:]
        if sub == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return (0, self.branch) if self.branch is not None else (128, "")
        if sub[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return (0, self.upstream) if self.upstream else (128, "")
        if sub[0] == "status":
            return 0, self.changes
        if sub[0] == "fetch":
            return (0, "") if self.fetch_ok else (1, "")
        if sub[0] == "rev-list":
            return 0, self.behind
        raise AssertionError(argv)


def test_git_state(tmp_path):
    assert update.git_state(tmp_path, FakeGit()) == update.GitState("main", "origin/main", False, 2)
    assert update.git_state(tmp_path, FakeGit(branch=None)) is None
    assert update.git_state(tmp_path, FakeGit(branch="HEAD")).branch is None
    assert update.git_state(tmp_path, FakeGit(upstream=None)).behind is None
    assert update.git_state(tmp_path, FakeGit(fetch_ok=False)).behind is None
    assert update.git_state(tmp_path, FakeGit(changes=" M src/x.py")).dirty
    no_fetch = FakeGit()
    update.git_state(tmp_path, no_fetch, fetch=False)
    assert ["fetch", "--quiet"] not in no_fetch.calls


def test_git_refusal(tmp_path):
    ok = update.GitState("main", "origin/main", False, 1)
    assert update.git_refusal(ok, tmp_path) is None
    assert "uncommitted" in update.git_refusal(replace(ok, dirty=True), tmp_path, "en")
    assert "detached" in update.git_refusal(replace(ok, branch=None), tmp_path, "en")
    assert "git branch -u origin/main" in update.git_refusal(replace(ok, upstream=None), tmp_path, "en")


def test_git_steps_per_os():
    linux = [s.argv for s in update.git_steps("linux", "uv", "zj")]
    assert linux == [["git", "pull", "--ff-only"], ["uv", "sync"],
                     ["systemctl", "--user", "try-restart", "zundamon-jarvis.service"]]
    mac = [s.argv for s in update.git_steps("mac", "uv", "zj", 501)]
    assert mac[2:] == [["launchctl", "kickstart", "-k", "gui/501/dev.zundamon-jarvis.daemon"],
                       ["launchctl", "kickstart", "-k", "gui/501/dev.zundamon-jarvis.menubar"]]
    win = update.git_steps("windows", "uv", "zj")
    assert win[0].argv == ["zj", "service", "stop"] and win[-1].argv == ["zj", "service", "start"] and win[-1].always


def test_run_steps_stops_at_a_failure_but_runs_the_always_ones(tmp_path):
    ran = []

    def run(argv, cwd):
        ran.append(argv[0])
        return 1 if argv[0] == "pull" else 0

    steps = [update.Step(["stop"], required=False), update.Step(["pull"]), update.Step(["sync"]),
             update.Step(["start"], required=False, always=True)]
    assert update.run_steps(steps, tmp_path, run) == ["pull"]
    assert ran == ["stop", "pull", "start"]
    assert update.run_steps(steps[2:], tmp_path, lambda a, c: 1 if a == ["start"] else 0) is None  # not required


# ---- `zundamon-jarvis update` ----

class World:
    def __init__(self, tmp_path, kind="uv-tool", installed="0.1.0", latest="v0.2.0", answer="", system="windows"):
        self.out, self.err, self.spawned, self.asked, self.ran = [], [], [], [], []
        self.data, self.logs = tmp_path / "data", tmp_path / "logs"
        self.latest = latest
        self.git = FakeGit()
        root = tmp_path / "repo"

        def ask(prompt):
            self.asked.append(prompt)
            if answer is EOFError:
                raise EOFError
            return answer

        self.deps = update.Deps(
            system=system, now=lambda: 50_000.0, fetch=lambda: self.latest, installed=lambda: installed,
            install=lambda: update.Install(kind, root if kind in ("git", "editable-tool") else tmp_path / "tool"),
            data_dir=lambda s: self.data, log_dir=lambda s: self.logs,
            find_uv=lambda: "/u/uv", find_exe=lambda s: "/u/zundamon-jarvis", uid=lambda: 501,
            git=self.git, run_visible=lambda argv, cwd: self.ran.append(argv) or 0,
            spawn=lambda argv, s, lp: self.spawned.append(argv), ask=ask,
            out=self.out.append, err=self.err.append, lang="en")

    def main(self, *args):
        return update.main(list(args), self.deps)

    def text(self):
        return "\n".join(self.out + self.err)


def test_check_up_to_date(tmp_path):
    w = World(tmp_path, latest="v0.1.0")
    assert w.main("--check") == 0
    assert w.out == ["Installed: 0.1.0", "Latest release: v0.1.0", "Up to date."]


def test_check_finds_an_update(tmp_path):
    w = World(tmp_path)
    assert w.main("--check") == 1
    assert "v0.2.0 is available." in w.out and not w.spawned and not w.asked


def test_check_unknown_is_2_and_uses_the_last_answer_when_there_is_one(tmp_path):
    w = World(tmp_path, latest=None)
    assert w.main("--check") == 2
    assert "Could not find out the latest release" in w.text()
    update.write_cache(w.data / update.CACHE, update.Known("v0.2.0", 40_000.0))
    w2 = World(tmp_path, latest=None)
    assert w2.main("--check") == 1
    assert any(line.startswith("Latest release (as of ") for line in w2.out)


def test_update_asks_then_starts_the_helper(tmp_path):
    w = World(tmp_path, answer="")
    assert w.main() == 0
    assert w.asked and "[Y/n]" in w.asked[0]
    assert w.spawned == [["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-WindowStyle", "Hidden", "-File", str(w.data / "update.ps1")]]
    assert "$zj = '/u/zundamon-jarvis'" in (w.data / "update.ps1").read_text(encoding="utf-8-sig")
    assert any("Updating in the background" in line for line in w.out)


def test_update_yes_asks_nothing(tmp_path):
    w = World(tmp_path, system="mac")
    assert w.main("--yes") == 0
    assert not w.asked and w.spawned == [["/bin/sh", str(w.data / "update.sh")]]


@pytest.mark.parametrize("answer", ["n", "no", EOFError])
def test_update_declined(tmp_path, answer):
    w = World(tmp_path, answer=answer)
    assert w.main() == 0
    assert not w.spawned and "Not updated." in w.out


def test_update_when_up_to_date_does_nothing(tmp_path):
    w = World(tmp_path, latest="v0.1.0")
    assert w.main("--yes") == 0 and not w.spawned


def test_a_second_update_while_one_runs_is_refused(tmp_path):
    w = World(tmp_path)
    w.data.mkdir(parents=True)
    (w.data / update.RUNNING).write_text("", encoding="utf-8")
    os.utime(w.data / update.RUNNING, (50_000 - 5, 50_000 - 5))
    assert w.main("--yes") == 2
    assert "already running" in w.text() and not w.spawned


def test_update_without_uv_or_exe(tmp_path):
    w = World(tmp_path)
    w.deps.find_uv = lambda: None
    assert w.main("--yes") == 2 and "uv was not found" in w.text() and "install.ps1" in w.text()

    def no_exe(s):
        raise RuntimeError("x")

    w2 = World(tmp_path)
    w2.deps.find_exe = no_exe
    assert w2.main("--yes") == 2 and not w2.spawned


def test_update_spawn_failure_is_1(tmp_path):
    w = World(tmp_path)

    def spawn(*a):
        raise OSError("blocked")

    w.deps.spawn = spawn
    assert w.main("--yes") == 1 and "blocked" in w.text()


def test_unknown_install_can_check_but_not_update(tmp_path):
    w = World(tmp_path, kind="unknown", system="mac")
    assert w.main("--check") == 1
    w2 = World(tmp_path, kind="unknown", system="mac")
    assert w2.main("--yes") == 2 and "install.sh" in w2.text() and not w2.spawned


def test_editable_tool_is_left_to_the_user(tmp_path):
    w = World(tmp_path, kind="editable-tool")
    assert w.main("--check") == 2 and "uv tool install --force -e" in w.text()


def test_usage(tmp_path):
    w = World(tmp_path)
    assert w.main("--force") == 2 and w.err == ["usage: zundamon-jarvis update [--check] [--yes]"]


def test_git_check_and_update(tmp_path):
    w = World(tmp_path, kind="git", system="linux")
    assert w.main("--check") == 1
    assert "origin/main has 2 new commit(s)." in w.out and not w.ran
    w2 = World(tmp_path, kind="git", system="linux")
    assert w2.main("--yes") == 0
    assert w2.ran == [["git", "pull", "--ff-only"], ["/u/uv", "sync"],
                      ["systemctl", "--user", "try-restart", "zundamon-jarvis.service"]]
    assert "Updated, and the service was restarted." in w2.out


def test_git_up_to_date_and_failures(tmp_path):
    w = World(tmp_path, kind="git", system="linux")
    w.git.behind = "0"
    assert w.main() == 0 and "Up to date with origin/main." in w.out and not w.ran
    w2 = World(tmp_path, kind="git", system="linux")
    w2.git.fetch_ok = False
    assert w2.main("--check") == 2 and "git fetch failed" in w2.text()
    w3 = World(tmp_path, kind="git", system="linux")
    w3.git.changes = " M README.md"
    assert w3.main("--yes") == 2 and "uncommitted" in w3.text() and not w3.ran
    w4 = World(tmp_path, kind="git", system="linux")
    w4.git.upstream = None
    assert w4.main("--check") == 2 and "tracks no remote branch" in w4.text()
    w5 = World(tmp_path, kind="git", system="linux")
    w5.deps.run_visible = lambda argv, cwd: 1 if argv[0] == "git" else 0
    assert w5.main("--yes") == 1 and "Failed: git pull --ff-only" in w5.text()


def test_cli_dispatches_update(monkeypatch):
    seen = []
    monkeypatch.setattr("zundamon_jarvis.update.main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["update", "--check"]) == 0
    assert seen == [["--check"]]
    assert "update" in cli.USAGE


# ---- menus, doctor ----

def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))


def test_menu_check(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    tool = lambda: update.Install("uv-tool", tmp_path)  # noqa: E731
    assert update.menu_check("linux", lambda: 1.0, tool, lambda: "v0.2.0", lambda: "0.1.0") == "v0.2.0"
    # cached now: a failing network still knows
    assert update.menu_check("linux", lambda: 2.0, tool, lambda: None, lambda: "0.1.0") == "v0.2.0"
    assert update.menu_check("linux", lambda: 3.0, tool, lambda: "v0.2.0", lambda: "0.2.0") is None
    git = lambda: update.Install("git", tmp_path)  # noqa: E731
    assert update.menu_check("linux", lambda: 1.0, git, lambda: "v9.0.0", lambda: "0.1.0") is None

    def broken():
        raise RuntimeError("x")

    assert update.menu_check("linux", lambda: 1.0, broken) is None


def test_watch_looks_after_the_first_delay_then_periodically():
    stop, seen = threading.Event(), []

    def on_result(tag):
        seen.append(tag)
        if len(seen) == 3:
            stop.set()

    update.watch(on_result, stop, look=lambda: "v0.2.0", first_s=0, every_s=0)
    assert seen == ["v0.2.0"] * 3
    stopped = threading.Event()
    stopped.set()
    update.watch(seen.append, stopped, look=lambda: "never", first_s=0)
    assert "never" not in seen


def test_launch_from_menu_runs_update_yes_detached_into_the_log(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    got = []
    update.launch_from_menu("windows", exe="zj.exe", spawn=lambda argv, s, lp: got.append((argv, s, lp)))
    assert got == [(["zj.exe", "update", "--yes"], "windows", tmp_path / "local" / "zundamon-jarvis" / "logs" / "update.log")]

    def fail(*a):
        raise OSError("x")

    update.launch_from_menu("windows", exe="zj.exe", spawn=fail)  # a menu click never raises


def test_doctor_line(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    tool = lambda: update.Install("uv-tool", tmp_path)  # noqa: E731
    assert update.doctor_line("linux", tool, "en") == "Update: uv-tool, latest release not checked yet"
    update.write_cache(update.cache_path("linux"), update.Known("v0.2.0", 1_000_000.0))
    line = update.doctor_line("linux", tool, "en")
    assert line.startswith("Update: uv-tool, latest release v0.2.0 (checked ")


@pytest.mark.parametrize("table", [update.TEXT, update.MENU_TEXT])
def test_every_string_in_both_languages_with_the_same_fields(table):
    import string

    for key, entry in table.items():
        assert all(entry.get(lang) for lang in LANGUAGES), key
        fields = {lang: {f for _, f, _, _ in string.Formatter().parse(entry[lang]) if f} for lang in LANGUAGES}
        assert len({frozenset(v) for v in fields.values()}) == 1, (key, fields)


def test_menu_text():
    assert update.menu_text("item", "v0.2.0", "en") == "Update to v0.2.0…"
    assert update.menu_text("item", "v0.2.0", "ja") == "v0.2.0 に更新…"


def test_spawn_detached_posix_starts_a_new_session(monkeypatch, tmp_path):
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

    monkeypatch.setattr(update.subprocess, "Popen", P)
    update.spawn_detached(["/bin/sh", "x"], "mac", tmp_path / "logs" / "update.log")
    assert seen["argv"] == ["/bin/sh", "x"] and seen["kw"]["start_new_session"] is True
    assert (tmp_path / "logs" / "update.log").exists()


def test_spawn_detached_windows_gives_a_hidden_console_outside_the_job(monkeypatch, tmp_path):
    flags = []

    class P:
        def __init__(self, argv, creationflags=0, **kw):
            flags.append(creationflags)
            if creationflags & update.CREATE_BREAKAWAY_FROM_JOB:
                raise OSError("job does not allow breaking away")

    monkeypatch.setattr(update.subprocess, "Popen", P)
    update.spawn_detached(["powershell.exe"], "windows", tmp_path / "update.log")
    assert flags[0] & update.CREATE_NO_WINDOW and flags[0] & update.CREATE_BREAKAWAY_FROM_JOB
    assert flags[1] == update.CREATE_NO_WINDOW | update.CREATE_NEW_PROCESS_GROUP
    assert not flags[0] & 0x00000008  # DETACHED_PROCESS: its console children would each open a window

