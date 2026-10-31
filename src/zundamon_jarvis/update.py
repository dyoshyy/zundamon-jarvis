"""`zundamon-jarvis update [--check] [--yes]`: find out whether a newer release exists and install it.

Two ways of installing are known, and each updates differently:

* **uv tool** (the Mac and Windows installers: ``uv tool install ... zundamon-jarvis @ <tag zip>``).
  "Latest" is the newest GitHub release (``releases/latest``'s ``tag_name``). Checking is cached
  in ``platform.data_dir()/update.json`` (the last successful answer and when it came), asked at
  most every ``MAX_AGE_S`` unless forced; a failed request (no network, rate limit, no release
  yet, the repository not public: 404) is quiet and keeps the last answer.
* **a git checkout** (Linux's documented install, and dev machines: ``uv sync`` in a clone, the
  package installed editable). "Latest" is the remote branch's head, not a tag: the check runs
  ``git fetch`` and counts the commits ``HEAD`` is behind its upstream. Updating is ``git pull
  --ff-only`` and ``uv sync`` in the foreground (it is run by hand in a terminal), then a
  restart of whatever service runs; it is refused, with what to do by hand, when the tree has
  changes, ``HEAD`` is detached or the branch tracks nothing. The menus never offer it.

A uv tool update cannot replace the running program's own files from inside them: Windows locks
a running exe and python, and the tray or the menu bar that asks for the update runs from the
very tool environment being replaced. So ``start_helper`` writes a small script (PowerShell on
Windows, ``/bin/sh`` elsewhere) next to the cache and starts it **detached**, then returns at
once; the script stops the service, runs ``uv tool install --force``, and starts the service
again whether or not the install worked (the user is never left without it). Its output goes to
``platform.log_dir()/update.log``. ``update.running`` beside the cache marks an update in flight,
so a second click does not start a second one.

Everything outside (the network, the clock, processes, file locations) comes in as parameters;
the version compare, the cache, the choice of plan and the scripts are pure and tested.
"""

import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from zundamon_jarvis import i18n, platform

log = logging.getLogger(__name__)

REPO = "dyoshyy/zundamon-jarvis"
DIST = "zundamon-jarvis"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
FETCH_TIMEOUT_S = 5.0
MAX_AGE_S = 12 * 3600  # the menus look every 6 h, so at most one request in 12 h
CACHE, LOG, RUNNING = "update.json", "update.log", "update.running"
RUNNING_STALE_S = 30 * 60  # an update marker older than this was left by a helper that died
INSTALL_TRIES = 3
FIRST_CHECK_S = 60.0  # the menus' first look, a minute after they start
CHECK_EVERY_S = 6 * 3600.0

INSTALLER = {
    "mac": "curl -fsSL https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.sh | sh",
    "windows": ('powershell -ExecutionPolicy Bypass -c "irm '
                'https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.ps1 | iex"'),
    "linux": "https://github.com/dyoshyy/zundamon-jarvis#linux",
}

# subprocess only defines these on Windows
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

TEXT: i18n.Table = {
    "usage": {"ja": "usage: zundamon-jarvis update [--check] [--yes]",
              "en": "usage: zundamon-jarvis update [--check] [--yes]"},
    "installed": {"ja": "入っている版: {version}", "en": "Installed: {version}"},
    "latest": {"ja": "最新の版: {tag}", "en": "Latest release: {tag}"},
    "latest_old": {"ja": "最新の版（{when} に確かめたもの）: {tag}",
                   "en": "Latest release (as of {when}): {tag}"},
    "latest_unknown": {
        "ja": "最新の版が分かりませんでした（ネットワークにつながらないか、まだリリースが無いか、リポジトリが公開されていません）。",
        "en": "Could not find out the latest release (no network, no release yet, or the repository is not public).",
    },
    "up_to_date": {"ja": "最新の版です。", "en": "Up to date."},
    "available": {"ja": "{tag} に更新できます。", "en": "{tag} is available."},
    "ask": {"ja": "{tag} に更新しますか？ 1 分ほど止まります。 [Y/n] ",
            "en": "Update to {tag} now? ZundamonJarvis stops for about a minute. [Y/n] "},
    "cancelled": {"ja": "更新しませんでした。", "en": "Not updated."},
    "started": {"ja": "裏で更新しています。終わると自動で起動し直します。ログ: {log}",
                "en": "Updating in the background; ZundamonJarvis starts again by itself when it is done. Log: {log}"},
    "busy": {"ja": "更新はもう始まっています。ログ: {log}", "en": "An update is already running. Log: {log}"},
    "no_uv": {"ja": "uv が見つかりません（PATH、~/.local/bin、UV）。インストーラをもう一度実行してください: {how}",
              "en": "uv was not found (PATH, ~/.local/bin, UV). Run the installer again instead: {how}"},
    "no_exe": {"ja": "zundamon-jarvis の実行ファイルが見つかりません。インストーラをもう一度実行してください: {how}",
               "en": "The zundamon-jarvis executable was not found. Run the installer again instead: {how}"},
    "spawn_failed": {"ja": "更新を始められませんでした: {error}", "en": "Could not start the update: {error}"},
    "unknown_install": {
        "ja": "インストーラ（uv tool）でも git の clone でも入れたものではないので、自分では更新できません。インストーラをもう一度実行してください: {how}",
        "en": "This copy was installed neither by the installer (uv tool) nor from a git checkout, so it cannot update itself. Run the installer again: {how}",
    },
    "editable_tool": {
        "ja": "{path} を編集可能な uv tool として入れています。そこで git pull してから uv tool install --force -e {path} を実行してください。",
        "en": "This is an editable uv tool install of {path}: run git pull there, then uv tool install --force -e {path}.",
    },
    "git_checkout": {"ja": "git の clone: {path}（ブランチ {branch}）", "en": "git checkout: {path} (branch {branch})"},
    "git_behind": {"ja": "{upstream} に新しいコミットが {count} 個あります。",
                   "en": "{upstream} has {count} new commit(s)."},
    "git_current": {"ja": "{upstream} と同じです。", "en": "Up to date with {upstream}."},
    "git_fetch_failed": {"ja": "git fetch に失敗したので、{upstream} に新しいコミットがあるか分かりません。",
                         "en": "git fetch failed, so it is unknown whether {upstream} has new commits."},
    "git_unusable": {"ja": "{path} で git が使えません。", "en": "git does not work in {path}."},
    "git_dirty": {
        "ja": "手元に未コミットの変更があるので更新しません。コミットか stash をしてから、{path} で git pull --ff-only と uv sync を実行してください。",
        "en": "The working tree has uncommitted changes, so it is not updated. Commit or stash them, then run git pull --ff-only and uv sync in {path}.",
    },
    "git_detached": {
        "ja": "ブランチに乗っていない（detached HEAD）ので更新しません。ブランチを checkout してから、git pull --ff-only と uv sync を実行してください。",
        "en": "Not on a branch (detached HEAD), so it is not updated. Check out a branch, then run git pull --ff-only and uv sync.",
    },
    "git_no_upstream": {
        "ja": "ブランチ {branch} は追跡するリモートのブランチが無いので更新しません（git branch -u origin/{branch} で設定できます）。",
        "en": "Branch {branch} tracks no remote branch, so it is not updated (git branch -u origin/{branch} sets one).",
    },
    "git_ask": {"ja": "git pull と uv sync をして、常駐を起動し直しますか？ [Y/n] ",
                "en": "Run git pull and uv sync, then restart the service? [Y/n] "},
    "git_failed": {"ja": "失敗しました: {cmd}", "en": "Failed: {cmd}"},
    "git_done": {"ja": "更新して、常駐を起動し直しました。", "en": "Updated, and the service was restarted."},
    "doctor": {"ja": "更新: {kind}、最新の版 {tag}", "en": "Update: {kind}, latest release {tag}"},
    "doctor_never": {"ja": "まだ確かめていません", "en": "not checked yet"},
    "doctor_when": {"ja": "{tag}（{when} に確認）", "en": "{tag} (checked {when})"},
}

# The menu bar's and the tray's update item (``menubar.menu_model(..., update=tag)``)
MENU_TEXT: i18n.Table = {
    "item": {"ja": "{tag} に更新…", "en": "Update to {tag}…"},
    "note": {"ja": "新しい版 {tag} があります。メニューの「{tag} に更新…」で更新できます（1 分ほど止まります）",
             "en": "Version {tag} is available: choose “Update to {tag}…” in this menu (it stops for about a minute)"},
}


def menu_text(key: str, tag: str, lang: str | None = None) -> str:
    return i18n.text(MENU_TEXT, key, lang, tag=tag)


# ---- versions ----

_VERSION = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def parse_version(s: str | None) -> tuple[int, ...] | None:
    """``"v0.1.0"`` / ``"0.1.0"`` -> ``(0, 1, 0)``; a suffix (``rc1``) is ignored; None if no number."""
    m = _VERSION.match((s or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(latest: str | None, installed: str | None) -> bool:
    a, b = parse_version(latest), parse_version(installed)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def installed_version(version: Callable[[str], str] = importlib.metadata.version) -> str | None:
    try:
        return version(DIST)
    except importlib.metadata.PackageNotFoundError:
        return None


def latest_release(urlopen: Callable[..., Any] = urllib.request.urlopen, url: str = LATEST_URL,
                   timeout: float = FETCH_TIMEOUT_S, user_agent: str | None = None) -> str | None:
    """The newest release's tag, or None for anything that goes wrong (404 while the repository
    is private or has no release, 403 at the rate limit, no network, a page that is not JSON)."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent or f"{DIST}/{installed_version() or '?'}",  # GitHub refuses requests without one
    })
    try:
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # HTTPError, URLError, timeouts, bad JSON, http.client oddities: all quiet
        log.debug("release check failed: %s", e)
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag.strip() if isinstance(tag, str) and tag.strip() else None


# ---- the cached answer ----

@dataclass(frozen=True)
class Known:
    latest: str | None = None  # the tag
    checked: float | None = None  # epoch seconds of the answer
    failed: bool = False  # a request was made just now and failed (the answer is the old one)


def cache_path(sys_: str | None = None) -> Path:
    return platform.data_dir(sys_) / CACHE


def read_cache(path: Path) -> Known:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Known()
    if not isinstance(data, dict):
        return Known()
    latest, checked = data.get("latest"), data.get("checked")
    return Known(latest if isinstance(latest, str) and latest else None,
                 float(checked) if isinstance(checked, (int, float)) else None)


def write_cache(path: Path, known: Known) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"latest": known.latest, "checked": known.checked}), encoding="utf-8")
    os.replace(tmp, path)


def check(path: Path, now: float, fetch: Callable[[], str | None] = latest_release,
          force: bool = False, max_age_s: float = MAX_AGE_S) -> Known:
    """The latest tag, asked of GitHub only when the cached answer is older than ``max_age_s``
    (or from the future: a clock that jumped back) or ``force``. A failed request keeps the old
    answer, marked ``failed``; it is not written, so the next look asks again."""
    old = read_cache(path)
    if not force and old.checked is not None and 0 <= now - old.checked < max_age_s:
        return old
    tag = fetch()
    if tag is None:
        return Known(old.latest, old.checked, failed=True)
    new = Known(tag, now)
    try:
        write_cache(path, new)
    except OSError as e:
        log.debug("cannot write %s: %s", path, e)
    return new


# ---- how this copy was installed ----

Kind = Literal["uv-tool", "git", "editable-tool", "unknown"]


@dataclass(frozen=True)
class Install:
    kind: Kind
    path: Path | None = None  # the tool environment, or the checkout's root


def _is_checkout(root: Path) -> bool:
    return (root / ".git").exists() and (root / "pyproject.toml").is_file()  # .git is a file in a worktree


def _uv_tool_dir() -> Path | None:
    uv = find_uv()
    if not uv:
        return None
    try:
        r = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=10,
                           encoding="utf-8", creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    return Path(out) if r.returncode == 0 and out else None


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def detect_install(prefix: Path | None = None, package_dir: Path | None = None,
                   uv_tool_dir: Callable[[], Path | None] = _uv_tool_dir) -> Install:
    """git: the running code is a checkout's ``src/zundamon_jarvis`` (``uv sync`` installs the
    project editable; ``PYTHONPATH=src`` imports it straight). uv tool: ``sys.prefix`` holds uv's
    ``uv-receipt.toml`` (or lies under ``uv tool dir``). An editable *tool* install of a checkout
    is both: its code comes from git, its dependencies from ``uv tool install``, so it is left
    to the user."""
    prefix = Path(sys.prefix) if prefix is None else prefix
    root = (package_dir or Path(__file__).resolve().parent).parent.parent
    tool = (prefix / "uv-receipt.toml").is_file()
    if _is_checkout(root):
        return Install("editable-tool" if tool else "git", root)
    if tool:
        return Install("uv-tool", prefix)
    tools = uv_tool_dir()
    if tools is not None and _within(prefix, tools):
        return Install("uv-tool", prefix)
    return Install("unknown", None)


def find_uv(environ: Mapping[str, str] | None = None, which: Callable[[str], str | None] = shutil.which,
            home: Path | None = None, sys_: str | None = None,
            is_file: Callable[[Path], bool] = Path.is_file) -> str | None:
    """``UV`` (uv sets it for what it runs), PATH, then where uv's installer puts it
    (``~/.local/bin``; ``%USERPROFILE%\\.local\\bin`` on Windows), cargo's, Homebrew's."""
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    exe = "uv.exe" if (sys_ or platform.system()) == "windows" else "uv"
    candidates: list[str | None] = [env.get("UV"), which("uv")]
    candidates += [str(home / ".local" / "bin" / exe), str(home / ".cargo" / "bin" / exe),
                   "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
    for c in candidates:
        if c and is_file(Path(c)):
            return os.path.normpath(c)
    return None


# ---- the uv tool update: a detached helper script ----

def source(tag: str) -> str:
    """What ``uv tool install`` installs: the tag's source zip (install.ps1's form; no git needed)."""
    return f"{DIST} @ https://github.com/{REPO}/archive/refs/tags/{tag}.zip"


def _sh(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


MAC_LABELS = ("dev.zundamon-jarvis.menubar", "dev.zundamon-jarvis.daemon")  # service.LABELS' Python ones


def windows_script(uv: str, exe: str, tag: str, marker: Path) -> str:
    """Stop the supervisor (``service stop`` waits until it is gone and kills it if it must),
    reinstall, start it again. The install is tried again after a pause: a Conversation or
    Settings window (``zundamon-jarvisctl``) may still hold the old files for a moment."""
    return "\n".join([
        "# zundamon-jarvis update helper, written by `zundamon-jarvis update` (safe to delete)",
        "$ErrorActionPreference = 'Continue'",
        "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}",
        "$env:PYTHONIOENCODING = 'utf-8'",
        f"$uv = {_ps(uv)}",
        f"$zj = {_ps(exe)}",
        f"$source = {_ps(source(tag))}",
        f"$marker = {_ps(str(marker))}",
        "function Say([string]$Text) { Write-Output ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Text) }",
        "Start-Sleep -Seconds 2  # let `zundamon-jarvis update` (a program of the tool being replaced) exit",
        "Say 'stopping the service'",
        "& $zj service stop 2>&1 | Out-String",
        "Start-Sleep -Seconds 2",
        "$ok = $false",
        f"foreach ($try in 1..{INSTALL_TRIES}) {{",
        "    Say \"uv tool install $source (try $try)\"",
        "    & $uv tool install --force --python 3.12 $source 2>&1 | Out-String",
        "    if ($LASTEXITCODE -eq 0) { $ok = $true; break }",
        "    Start-Sleep -Seconds 10",
        "}",
        f"if ($ok) {{ Say {_ps('installed ' + tag)} }} else {{ Say 'the install failed; starting what is installed' }}",
        "Say 'starting the service'",
        "& $zj service start 2>&1 | Out-String",
        "Remove-Item -LiteralPath $marker -ErrorAction SilentlyContinue",
        "if ($ok) { Say 'done'; exit 0 } else { exit 1 }",
        "",
    ])


def posix_script(sys_: str, uv: str, exe: str, tag: str, marker: Path, uid: int) -> str:
    """macOS: take the daemon and the menu bar out of launchd (the engine is not in the tool),
    reinstall, and ``service install`` (it bootstraps every agent again). Linux (a uv tool install
    there is not the documented one): reinstall and restart the unit if it runs."""
    if sys_ == "mac":
        stop = [f"launchctl bootout gui/{uid}/{label} 2>/dev/null" for label in MAC_LABELS]
        start = [f"{_sh(exe)} service install"]
    else:
        stop = []
        start = ["systemctl --user try-restart zundamon-jarvis.service"]
    return "\n".join([
        "#!/bin/sh",
        "# zundamon-jarvis update helper, written by `zundamon-jarvis update` (safe to delete)",
        "say() { printf '%s %s\\n' \"$(date '+%Y-%m-%d %H:%M:%S')\" \"$*\"; }",
        f"UV={_sh(uv)}",
        f"SOURCE={_sh(source(tag))}",
        f"MARKER={_sh(str(marker))}",
        "sleep 2  # let `zundamon-jarvis update` exit",
        *(["say 'stopping the service'", *stop] if stop else []),
        "ok=0",
        f"for try in {' '.join(str(i) for i in range(1, INSTALL_TRIES + 1))}; do",
        "  say \"uv tool install $SOURCE (try $try)\"",
        "  if \"$UV\" tool install --force --python 3.12 \"$SOURCE\"; then ok=1; break; fi",
        "  sleep 10",
        "done",
        f"if [ \"$ok\" = 1 ]; then say {_sh('installed ' + tag)}; else say 'the install failed; starting what is installed'; fi",
        "say 'starting the service'",
        *start,
        "rm -f \"$MARKER\"",
        "if [ \"$ok\" = 1 ]; then say done; exit 0; fi",
        "exit 1",
        "",
    ])


@dataclass(frozen=True)
class Helper:
    script: str
    path: Path  # where the script is written
    argv: list[str]  # what runs it
    encoding: str  # PowerShell 5.1 reads a BOM-less script in the ANSI code page


def helper(sys_: str, uv: str, exe: str, tag: str, data: Path, uid: int = 0) -> Helper:
    marker = data / RUNNING
    if sys_ == "windows":
        path = data / "update.ps1"
        return Helper(windows_script(uv, exe, tag, marker), path,
                      ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-WindowStyle", "Hidden", "-File", str(path)], "utf-8-sig")
    path = data / "update.sh"
    return Helper(posix_script(sys_, uv, exe, tag, marker, uid), path, ["/bin/sh", str(path)], "utf-8")


def in_progress(marker: Path, now: float) -> bool:
    try:
        return 0 <= now - marker.stat().st_mtime < RUNNING_STALE_S
    except OSError:
        return False


def spawn_detached(argv: Sequence[str], sys_: str, log_path: Path) -> None:
    """Start ``argv`` so it outlives us and whoever started us, output appended to ``log_path``.

    Windows: ``CREATE_NO_WINDOW`` gives it a hidden console of its own, which the console
    programs it runs (uv, zundamon-jarvis.exe) share; a ``DETACHED_PROCESS`` would have none, and
    each of them would open a visible console window. Out of our job if the job allows it (uv's
    launcher kills what its job holds when it exits). macOS/Linux: a new session, so launchd
    taking the menu bar's process group down does not take the helper with it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "ab")
    kw: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                              cwd=str(Path.home()), close_fds=True)
    try:
        if sys_ == "windows":
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(list(argv), creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                subprocess.Popen(list(argv), creationflags=flags, **kw)
        else:
            subprocess.Popen(list(argv), start_new_session=True, **kw)
    finally:
        out.close()  # the child has its own handle


def start_helper(h: Helper, sys_: str, log_path: Path, now: float, tag: str,
                 spawn: Callable[[Sequence[str], str, Path], Any] = spawn_detached) -> None:
    """Write the script and the in-flight marker, note the start in the log, and start it."""
    h.path.parent.mkdir(parents=True, exist_ok=True)
    h.path.write_text(h.script, encoding=h.encoding)
    marker = h.path.parent / RUNNING
    marker.write_text(f"{tag} {now:.0f}\n", encoding="utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} update to {tag} ({h.path})\n")
    try:
        spawn(h.argv, sys_, log_path)
    except OSError:
        marker.unlink(missing_ok=True)
        raise


# ---- the git checkout update: in the foreground ----

GitRun = Callable[[list[str], Path], tuple[int, str]]


def _git_run(argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "").strip()


@dataclass(frozen=True)
class GitState:
    branch: str | None  # None: detached HEAD
    upstream: str | None  # e.g. origin/main; None: the branch tracks nothing
    dirty: bool  # tracked files changed (untracked ones do not stop a fast-forward)
    behind: int | None  # commits on the upstream that HEAD lacks; None: unknown


def git_state(root: Path, run: GitRun = _git_run, fetch: bool = True) -> GitState | None:
    """None when git does not work there."""
    code, head = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    if code != 0:
        return None
    branch = None if head == "HEAD" else head
    code, up = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
    upstream = up if code == 0 and up else None
    _, changes = run(["git", "status", "--porcelain", "--untracked-files=no"], root)
    behind = None
    if branch and upstream:
        fetched = not fetch or run(["git", "fetch", "--quiet"], root)[0] == 0
        if fetched:
            code, count = run(["git", "rev-list", "--count", "HEAD..@{u}"], root)
            behind = int(count) if code == 0 and count.isdigit() else None
    return GitState(branch, upstream, bool(changes.strip()), behind)


def git_refusal(state: GitState, root: Path, lang: str | None = None) -> str | None:
    """Why the checkout must be updated by hand, or None."""
    if state.dirty:
        return i18n.text(TEXT, "git_dirty", lang, path=root)
    if state.branch is None:
        return i18n.text(TEXT, "git_detached", lang)
    if state.upstream is None:
        return i18n.text(TEXT, "git_no_upstream", lang, branch=state.branch)
    return None


@dataclass(frozen=True)
class Step:
    argv: list[str]
    required: bool = True  # a failure stops the steps after it (except ``always`` ones)
    always: bool = False  # runs even after a failure (start the service again)


def git_steps(sys_: str, uv: str, exe: str, uid: int = 0) -> list[Step]:
    """Pull, sync, restart whatever runs. Windows stops the supervisor first (its python holds
    the venv's files); launchd and systemd restart only what is loaded or active."""
    pre = [Step([exe, "service", "stop"], required=False)] if sys_ == "windows" else []
    if sys_ == "windows":
        post = [Step([exe, "service", "start"], required=False, always=True)]
    elif sys_ == "mac":
        post = [Step(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], required=False)
                for label in reversed(MAC_LABELS)]
    else:
        post = [Step(["systemctl", "--user", "try-restart", "zundamon-jarvis.service"], required=False)]
    return [*pre, Step(["git", "pull", "--ff-only"]), Step([uv, "sync"]), *post]


def run_steps(steps: Sequence[Step], cwd: Path, run: Callable[[list[str], Path], int]) -> list[str] | None:
    """Run the steps; the first required one that failed (its argv), or None if none did."""
    failed: list[str] | None = None
    for s in steps:
        if failed is not None and not s.always:
            continue
        if run(s.argv, cwd) != 0 and s.required and failed is None:
            failed = s.argv
    return failed


def _run_visible(argv: list[str], cwd: Path) -> int:
    print("$ " + " ".join(argv), flush=True)
    try:
        return subprocess.run(argv, cwd=str(cwd)).returncode
    except OSError as e:
        print(e, file=sys.stderr)
        return 1


# ---- the menus ----

def menu_check(sys_: str | None = None, now: Callable[[], float] = time.time,
               install: Callable[[], Install] = detect_install,
               fetch: Callable[[], str | None] = latest_release,
               installed: Callable[[], str | None] = installed_version) -> str | None:
    """The newer release's tag to offer in a menu, or None (also for a git checkout: that one is
    updated by hand). Cached; never raises."""
    try:
        if install().kind != "uv-tool":
            return None
        known = check(cache_path(sys_), now(), fetch)
        return known.latest if is_newer(known.latest, installed()) else None
    except Exception as e:
        log.debug("update check failed: %s", e)
        return None


def watch(on_result: Callable[[str | None], Any], stop: threading.Event,
          look: Callable[[], str | None] = menu_check,
          first_s: float = FIRST_CHECK_S, every_s: float = CHECK_EVERY_S) -> None:
    """Look ``first_s`` after starting, then every ``every_s``, until ``stop`` is set."""
    if stop.wait(first_s):
        return
    while True:
        on_result(look())
        if stop.wait(every_s):
            return


def start_watch(on_result: Callable[[str | None], Any], stop: threading.Event | None = None) -> threading.Thread:
    t = threading.Thread(target=watch, args=(on_result, stop or threading.Event()), daemon=True,
                         name="update-check")
    t.start()
    return t


def launch_from_menu(sys_: str | None = None, exe: str | None = None,
                     spawn: Callable[[Sequence[str], str, Path], Any] = spawn_detached) -> None:
    """The menu's "Update to …": ``zundamon-jarvis update --yes``, detached, its output in the log.
    It is a separate program so the menu (from the tool being replaced) asks nothing of itself."""
    s = sys_ or platform.system()
    if exe is None:
        from zundamon_jarvis.service import find_exe

        exe = find_exe(s)
    try:
        spawn([exe, "update", "--yes"], s, platform.log_dir(s) / LOG)
    except OSError as e:
        log.warning("cannot start the update: %s", e)


# ---- doctor ----

def doctor_line(sys_: str | None = None, install: Callable[[], Install] = detect_install,
                lang: str | None = None) -> str:
    """One line from the cache only (no network): how it was installed and the latest known tag."""
    known = read_cache(cache_path(sys_))
    if known.latest and known.checked is not None:
        tag = i18n.text(TEXT, "doctor_when", lang, tag=known.latest, when=_when(known.checked))
    else:
        tag = i18n.text(TEXT, "doctor_never", lang)
    try:
        kind = install().kind
    except Exception:
        kind = "unknown"
    return i18n.text(TEXT, "doctor", lang, kind=kind, tag=tag)


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


# ---- `zundamon-jarvis update` ----

def _find_exe(sys_: str) -> str:
    from zundamon_jarvis.service import find_exe

    return find_exe(sys_)


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


@dataclass
class Deps:
    """What ``main`` touches outside itself (the tests swap these)."""

    system: str = field(default_factory=platform.system)
    now: Callable[[], float] = time.time
    fetch: Callable[[], str | None] = latest_release
    installed: Callable[[], str | None] = installed_version
    install: Callable[[], Install] = detect_install
    data_dir: Callable[[str], Path] = platform.data_dir
    log_dir: Callable[[str], Path] = platform.log_dir
    find_uv: Callable[[], str | None] = find_uv
    find_exe: Callable[[str], str] = _find_exe
    uid: Callable[[], int] = _uid
    git: GitRun = _git_run
    run_visible: Callable[[list[str], Path], int] = _run_visible
    spawn: Callable[[Sequence[str], str, Path], Any] = spawn_detached
    ask: Callable[[str], str] = input
    out: Callable[[str], Any] = print
    err: Callable[[str], Any] = field(default_factory=lambda: lambda s: print(s, file=sys.stderr))
    lang: str | None = None


def _yes(d: Deps, prompt: str) -> bool:
    try:
        answer = d.ask(prompt)
    except EOFError:  # no terminal to answer from
        return False
    return answer.strip().lower() in ("", "y", "yes", "はい")


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    """Exit codes: ``--check`` 0 up to date, 1 an update exists, 2 unknown; otherwise 0 done (or
    nothing to do, or declined), 1 the update failed, 2 it cannot be done here."""
    d = deps or Deps()
    args = list(argv or [])
    if any(a not in ("--check", "--yes", "-y") for a in args):
        d.err(i18n.text(TEXT, "usage", d.lang))
        return 2
    check_only, yes = "--check" in args, ("--yes" in args or "-y" in args)
    t = lambda key, **f: i18n.text(TEXT, key, d.lang, **f)  # noqa: E731
    how = INSTALLER.get(d.system, INSTALLER["linux"])

    inst = d.install()
    if inst.kind == "git":
        return _git_main(d, inst.path or Path.cwd(), check_only, yes, t)
    if inst.kind == "editable-tool":
        d.err(t("editable_tool", path=inst.path))
        return 2

    installed = d.installed()
    d.out(t("installed", version=installed or "?"))
    known = check(d.data_dir(d.system) / CACHE, d.now(), d.fetch, force=True)
    if known.latest is None:
        d.err(t("latest_unknown"))
        return 2
    if known.failed and known.checked is not None:
        d.out(t("latest_old", tag=known.latest, when=_when(known.checked)))
    else:
        d.out(t("latest", tag=known.latest))
    if not is_newer(known.latest, installed):
        d.out(t("up_to_date"))
        return 0
    d.out(t("available", tag=known.latest))
    if check_only:
        return 1
    if inst.kind != "uv-tool":
        d.err(t("unknown_install", how=how))
        return 2
    data, log_path = d.data_dir(d.system), d.log_dir(d.system) / LOG
    if in_progress(data / RUNNING, d.now()):
        d.err(t("busy", log=log_path))
        return 2
    uv = d.find_uv()
    if not uv:
        d.err(t("no_uv", how=how))
        return 2
    try:
        exe = d.find_exe(d.system)
    except RuntimeError:
        d.err(t("no_exe", how=how))
        return 2
    if not yes and not _yes(d, t("ask", tag=known.latest)):
        d.out(t("cancelled"))
        return 0
    h = helper(d.system, uv, exe, known.latest, data, d.uid())
    try:
        start_helper(h, d.system, log_path, d.now(), known.latest, d.spawn)
    except OSError as e:
        d.err(t("spawn_failed", error=e))
        return 1
    d.out(t("started", log=log_path))
    return 0


def _git_main(d: Deps, root: Path, check_only: bool, yes: bool, t: Callable[..., str]) -> int:
    state = git_state(root, d.git)
    if state is None:
        d.err(t("git_unusable", path=root))
        return 2
    d.out(t("git_checkout", path=root, branch=state.branch or "HEAD"))
    if state.upstream is not None:
        if state.behind is None:
            d.err(t("git_fetch_failed", upstream=state.upstream))
            return 2
        if state.behind == 0:
            d.out(t("git_current", upstream=state.upstream))
            return 0
        d.out(t("git_behind", upstream=state.upstream, count=state.behind))
        if check_only:
            return 1
    refusal = git_refusal(state, root, d.lang)
    if refusal:
        d.err(refusal)
        return 2
    uv = d.find_uv()
    if not uv:
        d.err(t("no_uv", how=INSTALLER["linux"]))
        return 2
    try:
        exe = d.find_exe(d.system)
    except RuntimeError:
        exe = "zundamon-jarvis"
    if not yes and not _yes(d, t("git_ask")):
        d.out(t("cancelled"))
        return 0
    failed = run_steps(git_steps(d.system, uv, exe, d.uid()), root, d.run_visible)
    if failed is not None:
        d.err(t("git_failed", cmd=" ".join(failed)))
        return 1
    d.out(t("git_done"))
    return 0
