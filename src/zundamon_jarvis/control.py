"""Control socket: one text command per connection, one JSON line back.

Commands: ``toggle`` / ``pause`` / ``resume`` / ``status`` / ``quit`` (the daemon shuts down
cleanly and the reply adds ``"quitting": true``; Windows has no SIGTERM). Every reply is the status after
the command: ``{"state": "<State>", "text": "<short English label>"}`` (offline adds
the reason: ``Offline (mic)`` / ``Offline (voice)``, and ``"reasons": ["mic"|"tts", ...]``); an
unknown command gets ``{"error": "..."}``. ``text`` stays English (scripts read it); what is shown
on screen is ``status_label(status)``, in the display language (``zundamon_jarvis.i18n``). Stdlib only, so ``zundamon-jarvisctl`` (run by the status bar every few seconds)
starts fast.

Settings (``zundamon_jarvis.live_settings``; the settings window and ``zundamon-jarvisctl get|set``) reply with
the same status plus their own fields:

- ``get`` -> ``"settings": {speed, speaker_id, model, session_model, models, mic_device,
  mic_active, speed_min, speed_max, language}``
- ``set speed <0.8..2.0>`` / ``set speaker <style id>`` / ``set model sonnet|opus|haiku`` /
  ``set mic default|<source name>|<device index>`` (saved; applies on restart) /
  ``set language ja|en`` (saved; the windows and menus follow when they next draw) ->
  ``"ok": true, "saved": <written to config.toml>, "settings": {...}``
- ``preview <style id> <speed> <text>`` -> ``"ok": true`` (playing), or ``"ok": false,
  "busy": "user_speaking"|"speaking"|"replaced"`` (nothing played)
- ``newsession`` -> ``"session": "closed"|"none"|"busy"``
Bad input gets ``{"error": "..."}`` and changes nothing.

``debug`` adds ``"debug": {...}`` (``Daemon.debug``): paused, ears muted, frames in the last
5 s, last frame age, VAD prob max/mean over 2 s and 30 s, the endpointer (in speech, since
SpeechStarted, utterance length), the orchestrator's turn state, the offline reasons, the peak sample over 5 s (`max_abs_last_5s`),
the current run of all-zero frames (`zero_frames_streak_s`; 5 s reopens the mic) and `mic_fallback`.

``watch`` keeps the connection open and streams JSON lines (``WatchHub``): first
``{"kind": "hello", "boot": "<daemon run id>"}``, then the recent history (last 50 events,
in memory only), then ``{"kind": "live"}``, then every new event as it happens. Events carry
``id`` (counts up from 1 per daemon run), ``ts`` (local ISO time) and ``kind``. A watcher that
falls ``queue_max`` events behind is disconnected: publishing never waits for anyone.
``publish_live`` events (``hearing``) only go to the watchers connected now: they say what
is happening this instant, so they are never replayed as history.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from zundamon_jarvis import i18n, platform, transport

log = logging.getLogger(__name__)

STATE_TEXT = {  # UI labels are English; what ZundamonJarvis says follows the speaker's language
    "listening": "Listening",
    "paused": "Paused",
    "thinking": "Thinking",
    "speaking": "Speaking",
    "limit": "Limit",
    "offline": "Offline",
}
REASON_TEXT = {"mic": "mic", "tts": "voice"}
TEXT = {  # status_label: the same labels in the display language
    "listening": {"ja": "聞き取り中", "en": "Listening"},
    "paused": {"ja": "一時停止中", "en": "Paused"},
    "thinking": {"ja": "考え中", "en": "Thinking"},
    "speaking": {"ja": "話し中", "en": "Speaking"},
    "limit": {"ja": "利用上限", "en": "Limit"},
    "offline": {"ja": "オフライン", "en": "Offline"},
    "reason.mic": {"ja": "マイク", "en": "mic"},
    "reason.tts": {"ja": "音声", "en": "voice"},
    "with_reasons": {"ja": "{state}（{reasons}）", "en": "{state} ({reasons})"},
    "reason_sep": {"ja": "・", "en": ", "},
}
COMMANDS = ("toggle", "pause", "resume", "status", "debug", "quit")
SETTINGS_COMMANDS = ("get", "set", "preview", "newsession")
HISTORY_MAX = 50
WATCH_QUEUE_MAX = 256


class Controllable(Protocol):
    @property
    def state(self) -> str: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def toggle(self) -> None: ...


class Settings(Protocol):  # zundamon_jarvis.live_settings.LiveSettings
    def snapshot(self) -> dict: ...
    async def set(self, key: str, raw: str) -> dict: ...
    async def preview(self, speaker: str, speed: str, text: str) -> dict: ...
    def new_session(self) -> dict: ...


def socket_path() -> Path:
    return platform.socket_path()


def status_of(state: str, offline_reasons: Iterable[str] = (), login_ok: bool = True) -> dict:
    """The status; ``"login": false`` is added only while Claude rejects the login."""
    text = STATE_TEXT.get(state, state)
    reasons = sorted(REASON_TEXT.get(r, r) for r in offline_reasons)
    if state == "offline" and reasons:
        text = f"{text} ({', '.join(reasons)})"
    status = {"state": state, "text": text}
    if state == "offline" and reasons:
        status["reasons"] = sorted(offline_reasons)
    if not login_ok:
        status["login"] = False
    return status


def status_label(status: dict, lang: str | None = None) -> str:
    """The status (``status_of``'s dict) as shown on screen, in ``lang`` (default the display
    language); a state this version does not know shows the daemon's own ``text``."""
    state = str(status.get("state", ""))
    if state not in TEXT:
        return str(status.get("text") or state)
    lang = lang or i18n.language()
    label = i18n.text(TEXT, state, lang)
    reasons = [r for r in status.get("reasons") or () if isinstance(r, str)]
    if state == "offline" and reasons:
        names = [i18n.text(TEXT, f"reason.{r}", lang) if f"reason.{r}" in TEXT else r for r in sorted(reasons)]
        label = i18n.text(TEXT, "with_reasons", lang, state=label, reasons=i18n.text(TEXT, "reason_sep", lang).join(names))
    return label


def _status_of_target(target: "Controllable") -> dict:
    return status_of(target.state, getattr(target, "offline_reasons", ()), getattr(target, "login_ok", True))


async def _settings_command(line: str, settings: Settings | None) -> dict:
    words = line.split()
    cmd = words[0]
    if settings is None:
        return {"error": f"{cmd}: settings are not available"}
    if cmd == "get" and len(words) == 1:
        return {"settings": settings.snapshot()}
    if cmd == "newsession" and len(words) == 1:
        return settings.new_session()
    if cmd == "set" and len(words) == 3:
        return await settings.set(words[1], words[2])
    if cmd == "preview":
        parts = line.split(maxsplit=3)  # the text keeps its inner spaces
        if len(parts) == 4:
            return await settings.preview(parts[1], parts[2], parts[3])
    usage = {"get": "get", "newsession": "newsession", "set": "set speed|speaker|model|mic|language <value>",
             "preview": "preview <speaker id> <speed> <text>"}[cmd]
    return {"error": f"usage: {usage}"}


async def handle(line: str, target: Controllable, settings: Settings | None = None) -> dict:
    cmd = line.strip()
    first = cmd.split(maxsplit=1)[0] if cmd else ""
    if first in SETTINGS_COMMANDS:
        resp = await _settings_command(cmd, settings)
        if "error" in resp:
            return resp
        return {**_status_of_target(target), **resp}
    if cmd == "toggle":
        await target.toggle()
    elif cmd == "pause":
        await target.pause()
    elif cmd == "resume":
        await target.resume()
    elif cmd == "quit":  # Windows has no SIGTERM: the supervisor and `uninstall` stop it this way
        stop = getattr(target, "stop", None)
        if stop is None:
            return {"error": "quit: not available"}
        stop()
        return {**_status_of_target(target), "quitting": True}
    elif cmd == "debug":
        debug = getattr(target, "debug", None)
        if debug is None:
            return {"error": "debug: not available"}
        return {**_status_of_target(target), "debug": debug()}
    elif cmd != "status":
        return {"error": f"unknown command: {cmd!r} (use {'/'.join(COMMANDS)})"}
    return _status_of_target(target)


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


class _Watcher:
    def __init__(self, writer: asyncio.StreamWriter, queue_max: int) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(queue_max)
        self.stop: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def end(self, why: str) -> None:
        if not self.stop.done():
            self.stop.set_result(why)
        # A watcher stuck in drain() (its reader stopped reading) is cut off at once.
        self.writer.transport.abort()


class WatchHub:
    """Fans daemon events out to ``watch`` connections; keeps a short history in memory."""

    def __init__(
        self,
        history: int = HISTORY_MAX,
        queue_max: int = WATCH_QUEUE_MAX,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.boot = f"{os.getpid()}-{time.time_ns()}"
        self._now = now
        self._history: deque[bytes] = deque(maxlen=history)
        self._queue_max = queue_max
        self._watchers: set[_Watcher] = set()
        self._seq = 0

    @property
    def watchers(self) -> int:
        return len(self._watchers)

    def publish(self, kind: str, **fields: Any) -> dict | None:
        """Record an event and queue it for every watcher. Never blocks, never raises."""
        return self._publish(kind, fields, keep=True)

    def publish_live(self, kind: str, **fields: Any) -> dict | None:
        """Like ``publish``, but left out of the history."""
        return self._publish(kind, fields, keep=False)

    def _publish(self, kind: str, fields: dict[str, Any], keep: bool) -> dict | None:
        try:
            ev = {"id": self._seq + 1, "ts": self._now().isoformat(timespec="seconds"),
                  "kind": kind, **fields}
            data = _line(ev)
        except Exception:
            log.exception("watch event dropped (%s)", kind)
            return None
        self._seq += 1
        if keep:
            self._history.append(data)
        for w in list(self._watchers):
            try:
                w.queue.put_nowait(data)
            except asyncio.QueueFull:
                log.warning("a watcher fell behind; disconnecting it")
                self._watchers.discard(w)
                w.end("slow")
        return ev

    def close(self) -> None:
        for w in list(self._watchers):
            w.end("closing")
        self._watchers.clear()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        w = _Watcher(writer, self._queue_max)
        # Snapshot and subscribe with no await in between: nothing is missed or sent twice.
        writer.write(b"".join([_line({"kind": "hello", "boot": self.boot}), *self._history,
                               _line({"kind": "live"})]))
        self._watchers.add(w)
        gone = asyncio.ensure_future(reader.read(1))  # EOF (or anything): the watcher left
        try:
            await writer.drain()
            while True:
                get = asyncio.ensure_future(w.queue.get())
                done, _ = await asyncio.wait({get, gone, w.stop},
                                             return_when=asyncio.FIRST_COMPLETED)
                if get not in done:
                    get.cancel()
                    return
                writer.write(get.result())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self._watchers.discard(w)
            gone.cancel()


def is_live(path: Path) -> bool:
    return transport.is_live(path)


async def serve(
    target: Controllable,
    path: Path | None = None,
    hub: WatchHub | None = None,
    settings: Settings | None = None,
) -> asyncio.Server:
    path = path or socket_path()
    if path.exists() or path.is_symlink():
        if is_live(path):
            raise RuntimeError(f"zundamon-jarvis is already running ({path})")
        path.unlink()  # left over from a crash

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:  # connected and left: a liveness probe (is_live)
                return
            cmd = line.decode("utf-8", "replace")
            if cmd.strip() == "watch" and hub is not None:
                await hub.serve(reader, writer)
                return
            resp = await handle(cmd, target, settings)
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except ConnectionError:
            log.debug("control client went away")
        except Exception:
            log.exception("control request failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    return await transport.start_server(on_client, path)


def request(cmd: str, path: Path | None = None, timeout: float = 3.0) -> dict:
    """Send one command to the running daemon. Raises OSError if none is listening."""
    path = path or socket_path()
    deadline = time.monotonic() + timeout  # one budget for connect, send and read
    with transport.connect(path, timeout) as s:
        s.sendall(cmd.encode() + b"\n")
        chunks = []
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("control socket reply timed out")
            s.settimeout(left)
            if not (chunk := s.recv(4096)):
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks))
