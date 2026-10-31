"""Open or close the floating ``watch`` / ``settings`` windows, per desktop.

* Hyprland (``HYPRLAND_INSTANCE_SIGNATURE`` set and ``hyprctl`` present): by window class, via
  ``hyprctl`` and Omarchy's ``omarchy-launch-tui`` (the long-standing behavior).
* macOS: Terminal.app (or iTerm2 when it is running) through ``osascript``; the window is titled
  ``ZundamonJarvis — <name>`` and found by that title to close it.
* Any other Linux: ``$TERMINAL``, ``xdg-terminal-exec``, ``x-terminal-emulator`` in that order
  (opening only; there is no way to find the window again).
* Windows: a new Windows Terminal window (``wt -w new``) whose title is pinned to
  ``ZundamonJarvis — <name>`` (``--suppressApplicationTitle``), found again by that title
  (``FindWindowW``) and closed with ``WM_CLOSE``. Without Windows Terminal (older Windows 10) the
  command gets a classic console of its own (``CREATE_NEW_CONSOLE``); the program inside retitles
  that one, so it is opening only.
"""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any, Literal

from zundamon_jarvis import platform

log = logging.getLogger(__name__)
TITLE = "ZundamonJarvis"
CLASSES = {"watch": "zundamon-jarvis-watch", "settings": "zundamon-jarvis-settings"}


def _title(name: str) -> str:
    return f"{TITLE} — {name}"


def _spawn_detached(cmd: list[str]) -> None:
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


def _toggle_hyprland(app_id: str, argv: list[str], run: Callable[..., Any], spawn: Callable[..., Any]) -> None:
    """Close every window of class ``app_id``, or launch ``argv`` in a terminal with that class."""
    listing = run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5)
    try:
        clients = json.loads(listing.stdout or "[]")
    except ValueError:
        clients = []
    found = [c.get("address") for c in clients if isinstance(c, dict) and c.get("class") == app_id]
    if found:
        for addr in found:
            r = run(["hyprctl", "dispatch", f'hl.dsp.window.close({{ window = "address:{addr}" }})'],
                    capture_output=True, text=True, timeout=5)
            if (r.stdout or "").strip() != "ok":  # a pre-Lua Hyprland
                run(["hyprctl", "dispatch", "closewindow", f"address:{addr}"],
                    capture_output=True, text=True, timeout=5)
        return
    launcher = shutil.which("omarchy-launch-tui")
    if launcher:
        cmd = [launcher, f"--app-id={app_id}", *argv]
    else:
        cmd = ["xdg-terminal-exec", f"--app-id={app_id}", "-e", *argv]
    spawn(cmd)


# ---- macOS ----

def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(run: Callable[..., Any], script: str) -> str:
    r = run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if getattr(r, "returncode", 0):
        log.warning("osascript failed (%s): %s", r.returncode, (getattr(r, "stderr", "") or "").strip())
    return (r.stdout or "").strip()


def _toggle_mac(name: str, argv: list[str], run: Callable[..., Any]) -> None:
    title = _applescript_quote(_title(name))
    cmd = _applescript_quote(shlex.join(argv))
    iterm = _osascript(run, 'application "iTerm" is running') == "true"
    app = "iTerm" if iterm else "Terminal"
    # Counting windows only reads; the app must already be running or the tell would launch it.
    count = _osascript(
        run, f'tell application "{app}" to count (every window whose name contains {title})'
    ) if iterm or _osascript(run, 'application "Terminal" is running') == "true" else "0"
    if count.isdigit() and int(count) > 0:
        # End the TUI first: closing a window that still runs a process makes Terminal ask
        # "terminate running processes?" and the close would never complete.
        run(["pkill", "-f", re.escape(shlex.join(argv))], capture_output=True, text=True, timeout=5)
        saving = " saving no" if not iterm else ""
        _osascript(run, f'tell application "{app}" to close (every window whose name contains {title}){saving}')
        return
    if iterm:
        script = (f'tell application "iTerm"\n  activate\n'
                  f'  set w to (create window with default profile command {cmd})\n'
                  f'  tell current session of w to set name to {title}\nend tell')
    else:
        # No `activate` before `do script`: it launches Terminal with exactly one window.
        script = (f'tell application "Terminal"\n  set t to do script {cmd}\n'
                  f'  set custom title of t to {title}\n  activate\nend tell')
    _osascript(run, script)


# ---- generic Linux ----

def _generic_terminal() -> list[str] | None:
    """The terminal command prefix that takes the program to run after it, or None."""
    term = os.environ.get("TERMINAL")
    path = shutil.which(term) if term else None
    if path:
        return [path, "-e"]
    path = shutil.which("xdg-terminal-exec")
    if path:
        return [path]  # takes the command directly
    path = shutil.which("x-terminal-emulator")
    if path:
        return [path, "-e"]
    return None


# ---- Windows ----

# subprocess only defines these on Windows
CREATE_NEW_CONSOLE = 0x00000010
WM_CLOSE = 0x0010


def _spawn_windows(cmd: list[str], creationflags: int = 0) -> None:
    # No std handles: given any, a new console's program would write to ours (a log file under the tray).
    subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)


def _find_window(title: str) -> int:
    import ctypes

    return int(ctypes.windll.user32.FindWindowW(None, title) or 0)  # type: ignore[attr-defined]


def _close_window(hwnd: int) -> None:
    import ctypes

    ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # type: ignore[attr-defined]


def _toggle_windows(name: str, argv: list[str], spawn: Callable[..., Any],
                    find_window: Callable[[str], int], close_window: Callable[[int], Any]) -> None:
    title = _title(name)
    hwnd = find_window(title)
    if hwnd:
        close_window(hwnd)
        return
    wt = shutil.which("wt")
    if wt:
        spawn([wt, "-w", "new", "new-tab", "--title", title, "--suppressApplicationTitle", *argv])
    else:
        spawn(argv, creationflags=CREATE_NEW_CONSOLE)


def toggle_window(
    name: Literal["watch", "settings"],
    argv: list[str],
    system: str | None = None,
    run: Callable[..., Any] = subprocess.run,
    spawn: Callable[..., Any] | None = None,
    find_window: Callable[[str], int] = _find_window,
    close_window: Callable[[int], Any] = _close_window,
) -> None:
    """Toggle window ``name`` running ``argv`` (absolute command + args) for this desktop.

    With no terminal available (generic Linux) prints the command to run and exits 1.
    """
    sys_ = system or platform.system()
    if sys_ == "mac":
        _toggle_mac(name, argv, run)
        return
    if sys_ == "windows":
        _toggle_windows(name, argv, spawn or _spawn_windows, find_window, close_window)
        return
    spawn = spawn or _spawn_detached
    if _hyprland():
        _toggle_hyprland(CLASSES[name], argv, run, spawn)
        return
    prefix = _generic_terminal()
    if prefix is None:
        print("no terminal found; run this yourself: " + shlex.join(argv), file=sys.stderr)
        raise SystemExit(1)
    spawn([*prefix, *argv])
