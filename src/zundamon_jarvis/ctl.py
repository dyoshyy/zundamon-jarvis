"""`zundamon-jarvisctl toggle|pause|resume|status|get|set|bar|watch|window|settings` — talk to the daemon.

``debug`` prints where the ears are (see ``zundamon_jarvis.control``); ``get`` prints the status
and the live settings; ``set speed|speaker|model|mic <value>`` changes one (live, and in
config.toml; see ``zundamon_jarvis.control``). ``settings`` runs the settings TUI
(``zundamon_jarvis.settings_app``, imported only then: the bar's ``zundamon-jarvisctl bar`` stays light).

``bar`` prints Waybar-style JSON for the Omarchy shell's command bar module, and nothing
at all when the daemon is not running (the module hides on empty output; ``{"text": ""}``
would be shown verbatim, since the shell falls back to the raw output).

``watch`` streams the conversation (control socket ``watch``): recent history, then live
lines; while the daemon is away it says so once and reconnects every 2 s, skipping what it has
already shown (same daemon run) or showing everything again (a restarted daemon). Ctrl-C ends it.

``character [state]`` runs the on-screen character overlay (``zundamon_jarvis.character_app``) under
the system python, which has GTK; the optional state previews one animation without a daemon.

``window [name]`` toggles a terminal window (``zundamon_jarvis.desktop`` picks how: Hyprland by
window class, macOS Terminal/iTerm2, or any Linux terminal). ``WINDOWS`` lists the known ones
(``watch`` runs ``zundamon-jarvisctl watch``, ``settings`` runs ``zundamon-jarvisctl settings``); the
Hyprland window rule for each class (``desktop.CLASSES``: float, size, position) lives in the
user's Hyprland config.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from zundamon_jarvis import desktop
from zundamon_jarvis.control import COMMANDS, request, socket_path

USAGE = ("usage: zundamon-jarvisctl toggle|pause|resume|status|debug|get|bar|watch|settings"
         "|set speed|speaker|model|mic <value>|window [watch|settings]|character [state]")
HOTKEY = "SUPER+ALT+J"
ICON = {
    "listening": "\U000f036c",  # nf-md-microphone
    "paused": "\U000f036d",  # nf-md-microphone_off
    "thinking": "\U000f051f",  # nf-md-timer_sand
    "speaking": "\U000f057e",  # nf-md-volume_high
    "limit": "\U000f0026",  # nf-md-alert
    "offline": "\U000f0b5a",  # nf-md-cloud_off_outline
}


def bar_output(resp: dict) -> dict:
    state, text = resp.get("state", ""), resp.get("text", "")
    icon = ICON.get(state, "")
    return {
        "text": f"{icon} {text}".strip(),
        "tooltip": (f"ZundamonJarvis: {text} — click: conversation, right-click: settings, "
                    f"{HOTKEY}: pause/resume"),
        "class": state,
    }


# ---- watch ----

RETRY_S = 2.0
LIVE_LINE = "──── live from here ────"
RESTART_LINE = "──── ZundamonJarvis restarted ────"
WAITING_LINE = "⏳ ZundamonJarvis is not running. Waiting for it to start…"
WINDOW_TITLE = "ZundamonJarvis"

_RESET = "\033[0m"
_STYLE = {
    "heard": "\033[1;36m",  # bold cyan
    "reply": "\033[32m",  # green
    "dim": "\033[2m",
    "warn": "\033[33m",  # yellow
    "ok": "\033[32m",
    "no": "\033[31m",  # red
}
STATE_ICON = {"paused": "⏸", "listening": "▶", "limit": "⛔", "offline": "📴"}
_CONFIRM = {
    (True, "answer"): ("ok", "✅ Yes → go"),
    (False, "answer"): ("no", "❌ No → cancelled"),
    (False, "timeout"): ("no", "⌛ No answer → cancelled"),
    (False, "unclear"): ("no", "❔ Not understood → cancelled"),
}
_TASK = {"completed": "🧵 Background task finished", "failed": "🧵 Background task failed", "stopped": "🧵 Background task stopped"}


def _hhmm(ts: Any) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (TypeError, ValueError):
        return "--:--"


def _body(ev: dict) -> tuple[str, str] | None:
    """(style, text) for one event, or None for kinds not shown."""
    kind = ev.get("kind")
    text = str(ev.get("text", ""))
    if kind == "heard":
        return "heard", f"You ▸ {text}"
    if kind == "said":
        what = ev.get("what")
        if what == "question":
            return "warn", f"❓ Confirm: {text}"
        if what == "filler":
            return "dim", f"ZundamonJarvis ▸ ({text})"
        return ("warn" if what == "notice" else "reply"), f"ZundamonJarvis ▸ {text}"
    if kind == "confirm":
        ok = bool(ev.get("ok"))
        return _CONFIRM.get((ok, ev.get("why")), ("ok", "✅ Yes → go") if ok else ("no", "❌ Cancelled"))
    if kind == "tool":
        return "dim", f"  {'bg: ' if ev.get('sub') else ''}{ev.get('label', '')}"
    if kind == "state":
        state = ev.get("state")
        style = "dim" if state == "listening" else "warn"
        return style, f"{STATE_ICON.get(state, '•')} {text or state}"
    if kind == "task":
        head = _TASK.get(ev.get("status"), f"🧵 Background task: {ev.get('status')}")
        summary = ev.get("summary")
        return "dim", f"{head}: {summary}" if summary else head
    return None


def format_event(ev: dict, color: bool = False) -> str | None:
    body = _body(ev)
    if body is None:
        return None
    style, text = body
    line = f"{_hhmm(ev.get('ts'))} {text}"
    return f"{_STYLE[style]}{line}{_RESET}" if color else line


class WatchView:
    """Turns the stream (across reconnects) into the lines to print."""

    def __init__(self, color: bool = False) -> None:
        self.color = color
        self._boot: str | None = None
        self._last_id = 0
        self._live_shown = False
        self._waiting = False
        self._thinking = False

    @property
    def busy(self) -> bool:
        """True while the brain is working on a turn (live only, not while replaying history)."""
        return self._thinking and self._live_shown

    def _dim(self, line: str) -> str:
        return f"{_STYLE['dim']}{line}{_RESET}" if self.color else line

    def feed(self, ev: dict) -> list[str]:
        kind = ev.get("kind")
        if kind == "hello":
            self._waiting = False
            self._thinking = False
            boot = ev.get("boot")
            lines = []
            if self._boot is not None and boot != self._boot:
                lines.append(self._dim(RESTART_LINE))
                self._last_id = 0
                self._live_shown = False
            self._boot = boot
            return lines
        if kind == "live":
            if self._live_shown:
                return []
            self._live_shown = True
            return [self._dim(LIVE_LINE)]
        ev_id = ev.get("id")
        if isinstance(ev_id, int):
            if ev_id <= self._last_id:
                return []  # already shown before the reconnect
            self._last_id = ev_id
        if kind == "thinking":
            self._thinking = bool(ev.get("on"))
            return []
        self._thinking = False  # any other event means the turn moved on
        line = format_event(ev, self.color)
        return [line] if line is not None else []

    def disconnected(self) -> str | None:
        self._thinking = False
        if self._waiting:
            return None
        self._waiting = True
        return WAITING_LINE


class Loader:
    """A one-line spinner at the bottom of the window while ``view.busy``; lines scroll above it."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    TICK_S = 0.1
    CLEAR = "\r\033[K"

    def __init__(self, out: TextIO, view: "WatchView") -> None:
        self._out = out
        self._view = view
        self._lock = threading.Lock()
        self._shown = False
        self._frame = 0

    def tick(self) -> None:
        with self._lock:
            if self._view.busy:
                frame = self.FRAMES[self._frame % len(self.FRAMES)]
                self._frame += 1
                text = f"{frame} ZundamonJarvis is thinking…"
                self._out.write(self.CLEAR + (self._view._dim(text)))
                self._out.flush()
                self._shown = True
            else:
                self._erase()

    def line(self, text: str) -> None:
        with self._lock:
            self._erase()
            print(text, file=self._out, flush=True)

    def _erase(self) -> None:
        if self._shown:
            self._out.write(self.CLEAR)
            self._out.flush()
            self._shown = False

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self.TICK_S):
            self.tick()
        with self._lock:
            self._erase()


def _stream(path: Path, view: WatchView, emit: Callable[[str], None]) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(path))
        s.sendall(b"watch\n")
        s.settimeout(None)
        with s.makefile("rb") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    for line in view.feed(ev):
                        emit(line)


def watch(
    path: Path | None = None,
    out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    color: bool | None = None,
) -> int:
    out = out or sys.stdout
    path = path or socket_path()
    tty = out.isatty()
    if color is None:
        color = tty and "NO_COLOR" not in os.environ
    view = WatchView(color)

    loader = Loader(out, view) if tty else None
    stop = threading.Event()

    def emit(line: str) -> None:
        if loader is not None:
            loader.line(line)
        else:
            print(line, file=out, flush=True)

    try:
        if tty:
            out.write(f"\033]0;{WINDOW_TITLE}\007")
        if loader is not None:
            threading.Thread(target=loader.run, args=(stop,), daemon=True).start()
        while True:
            try:
                _stream(path, view, emit)
            except OSError:
                pass  # not running, or it went away
            line = view.disconnected()
            if line is not None:
                emit(line)
            sleep(RETRY_S)
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()


# ---- window ----

WINDOWS: dict[str, list[str]] = {
    # name: zundamon-jarvisctl arguments to run in it (the Hyprland class lives in desktop.CLASSES)
    "watch": ["watch"],
    "settings": ["settings"],
}


def _zundamon_jarvisctl() -> str:
    """This venv's zundamon-jarvisctl, by absolute path (the bar runs commands without our PATH)."""
    exe = Path(sys.executable).with_name("zundamon-jarvisctl")
    return str(exe) if exe.exists() else (shutil.which("zundamon-jarvisctl") or "zundamon-jarvisctl")


def open_window(name: str) -> int:
    if name not in WINDOWS:
        print(f"unknown window: {name!r} (use {'/'.join(WINDOWS)})", file=sys.stderr)
        return 2
    try:
        desktop.toggle_window(name, [_zundamon_jarvisctl(), *WINDOWS[name]])  # type: ignore[arg-type]
    except SystemExit as e:  # no terminal found: desktop already printed the command
        return int(e.code or 0)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"zundamon-jarvisctl window: {e}", file=sys.stderr)
        return 1
    return 0


# ---- character ----

SYSTEM_PYTHON = "/usr/bin/python3"  # GTK's bindings (python-gobject) are system packages
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"


def character_command(preview: str | None = None) -> tuple[list[str], dict[str, str]]:
    """The system-python command and extra environment that run the character overlay."""
    src = str(Path(__file__).resolve().parent.parent)
    env = {"PYTHONPATH": src}
    if Path(LAYER_SHELL_LIB).exists():  # layer-shell must be loaded before libwayland-client
        env["LD_PRELOAD"] = LAYER_SHELL_LIB
    return [SYSTEM_PYTHON, "-m", "zundamon_jarvis.character_app", *([preview] if preview else [])], env


def _character(preview: str | None, execve: Callable[..., Any] = os.execve) -> int:
    cmd, extra = character_command(preview)
    if not Path(cmd[0]).exists():
        print(f"zundamon-jarvisctl character: {cmd[0]} not found", file=sys.stderr)
        return 1
    execve(cmd[0], cmd, {**os.environ, **extra})
    return 0  # only reached by a fake execve


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["watch"]:
        return watch()
    if args == ["settings"]:
        from zundamon_jarvis.settings_app import main as settings_main

        return settings_main()
    if args[:1] == ["character"] and len(args) <= 2:
        return _character(args[1] if len(args) == 2 else None)
    if args[:1] == ["window"] and len(args) <= 2:
        return open_window(args[1] if len(args) == 2 else "watch")
    if args[:1] == ["set"] and len(args) == 3:
        cmd = " ".join(args)
    elif len(args) == 1 and args[0] in (*COMMANDS, "get", "bar"):
        cmd = args[0]
    else:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        resp = request("status" if cmd == "bar" else cmd)
    except (OSError, ValueError):  # not running (or a garbled reply)
        if cmd == "bar":
            return 0
        print(json.dumps({"state": "off", "text": "Stopped"}, ensure_ascii=False))
        return 1
    if cmd == "bar":
        print(json.dumps(bar_output(resp), ensure_ascii=False))
        return 0
    print(json.dumps(resp, ensure_ascii=False))
    return 2 if "error" in resp else 0
