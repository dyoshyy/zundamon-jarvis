"""`zundamon-jarvis setup`: the setup steps as a local web page (``--cli`` keeps the terminal wizard).

Why a web page: one UI for macOS, Windows and Linux with nothing new to install. The server is
the stdlib ``http.server`` inside the setup process, bound to 127.0.0.1 on a free port; the page
is static HTML/CSS/JS shipped in ``assets/setup/`` (no CDN, no build step). On macOS and Windows
it is shown in an app window of its own (pywebview: WKWebView / WebView2, which the OS already
has), so setup looks like an app rather than a browser tab; closing the window ends setup. On
Linux, or when no window can be made, ``webbrowser.open`` shows it instead and the terminal
prints the URL in case no browser comes up.

The steps are the terminal wizard's (``zundamon_jarvis.setup_wizard``, whose step helpers both
use, so what is saved and said stays the same) plus the two big downloads the installers used to
run in the terminal: the VOICEVOX engine and the speech-to-text model, with real progress.
Every answer is written to ``config.toml`` as soon as it is given; a re-run shows current values.

Layers, so the server logic is testable without a browser, the network or the real machine:

- ``SetupSession``: the state of one setup run and the work behind each step. Long work (the
  downloads, the login check, the mic meter, VOICEVOX, starting the services) runs on worker
  threads or on the session's own asyncio loop thread; requests only start it and read state,
  and the page polls ``/api/state``. Everything outside comes in through ``Deps``.
- ``handle(app, request) -> Response``: a pure function of the request: the security checks,
  the routing and the JSON. ``make_server`` wraps it in a ``ThreadingHTTPServer``.
- ``main``: wiring, the browser, and when to exit (finished, the page gone, or Ctrl-C).

Security: only 127.0.0.1 is bound; the URL carries a random token, which the first load trades
for an HttpOnly SameSite=Strict cookie that every other request must carry; a ``Host`` that is not
``127.0.0.1:<port>`` / ``localhost:<port>`` is refused (DNS rebinding), as is a foreign
``Origin``; a POST must be ``application/json`` (a cross-site form cannot send that without a
CORS preflight, which is never answered) and the page's CSP allows only its own files.
"""

import asyncio
import contextlib
import hmac
import http.cookies
import json
import logging
import math
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from zundamon_jarvis import config as config_mod
from zundamon_jarvis import platform
from zundamon_jarvis import setup_wizard as wiz
from zundamon_jarvis.config import MODELS, SPEED_MAX, SPEED_MIN, Config, update_config, valid_speed
from zundamon_jarvis.tts import talk_styles

log = logging.getLogger(__name__)

COOKIE = "zj_setup"
GONE_S = 180.0  # no request for this long: the page is gone (background tabs poll about once a minute)
BYE_S = 15.0  # after the page said goodbye (closed or reloading): a reload comes back well within this
FIRST_CONTACT_S = 30 * 60.0  # the URL may have to be copied from the terminal by hand
SILENT_AFTER_S = 3.0  # the meter read nothing but zeros this long: say so
MAX_BODY = 64 * 1024
PICK_TIMEOUT_S = 600.0

LOGIN_HELP = "Claude にログインできていません（Claude Code が入っていないときも、こうなります）。"
DEFAULT_MIC_LABEL = "OS の既定のマイク"
MIC_FALLBACK_NOTE = "選んだマイクが見つからないので、OS の既定のマイクの音を表示しています。"
START_PENDING = "起動中です。少し待ってから、メニューバー（Windows は通知領域）のアイコンを見てください。"


# ---- outside dependencies ----

@dataclass
class Deps:
    """Everything outside the session. ``default_deps`` fills in the real ones for an OS."""

    login_check: Callable[[str | None], Awaitable[str]]
    voice_engine: Callable[[Callable[..., None]], Any]  # (say) -> async CM yielding a Voicevox or None
    play: Callable[[Any], Awaitable[None]]
    start_services: Callable[[], None]
    request: Callable[..., dict]
    audio_factory: Callable[..., Any]  # AudioIn(device=...)
    list_mics: Callable[[], list]  # [mics.MicSource]
    default_mic: Callable[[], str | None]  # the OS default input's name
    engine_installed: Callable[[], bool]
    install_engine: Callable[[Callable[[str, int, int | None], None]], Any]  # (progress)
    model_installed: Callable[[], bool]
    download_model: Callable[[Callable[[int, int | None], None]], Any]  # (progress)
    pick_folder: Callable[[], str | None] | None = None  # None: no native dialog (no tkinter)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def _model_installed(sys_: str) -> bool:
    from zundamon_jarvis import stt

    try:
        stt.download_model(sys_, local_only=True)
        return True
    except Exception:  # not in the cache (LocalEntryNotFoundError), or no huggingface_hub
        return False


PICK_SCRIPT = """
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(title=sys.argv[1], mustexist=True)
sys.stdout.write(path or "")
"""


def tk_folder_picker() -> Callable[[], str | None] | None:
    """A native folder dialog, or None when tkinter is missing. It runs in a child python, so
    Tk gets a main thread of its own (macOS requires one) and a crash cannot take setup down."""
    import importlib.util

    if importlib.util.find_spec("tkinter") is None:
        return None

    def pick() -> str | None:
        try:
            r = subprocess.run([sys.executable, "-c", PICK_SCRIPT, "知識フォルダを選ぶ"],
                               capture_output=True, text=True, timeout=PICK_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            log.debug("folder dialog failed", exc_info=True)
            return None
        return r.stdout.strip() or None

    return pick


def default_deps(sys_: str) -> Deps:
    from zundamon_jarvis import engine, mics, stt

    def audio(**kw: Any) -> Any:
        from zundamon_jarvis.audio_in import AudioIn

        return AudioIn(system=sys_, **kw)

    return Deps(
        login_check=wiz.claude_login_check,
        voice_engine=lambda say: wiz.open_voice_engine(say, sys_),
        play=wiz.play_pcm,
        start_services=lambda: wiz.install_and_start(sys_),
        request=wiz._request,
        audio_factory=audio,
        list_mics=lambda: mics.list_sources(system=sys_),
        default_mic=lambda: mics.default_source_name(system=sys_),
        engine_installed=lambda: engine._installed(engine.default_dest(sys_), sys_),
        install_engine=lambda progress: engine.install_engine(sys_, progress=progress),
        model_installed=lambda: _model_installed(sys_),
        download_model=lambda progress: stt.download_model(sys_, progress=progress),
        pick_folder=tk_folder_picker(),
    )


# ---- the session ----

class Refused(Exception):
    """A step that cannot be done now (HTTP 409 with this message)."""


@dataclass
class _Download:
    state: str = "checking"  # checking | waiting | running | done | error
    stage: str | None = None  # engine: download | extract | check
    done: int = 0
    total: int | None = None
    error: str | None = None


@dataclass
class _Mic:
    choices: list = field(default_factory=list)  # [(device, label)]
    state: str = "idle"  # idle | opening | listening | error
    level: float = 0.0
    peak: float = 0.0
    opened_at: float = 0.0
    fallback: bool = False


@dataclass
class _Voice:
    state: str = "idle"  # idle | waiting | starting | ready | down
    message: str | None = None
    choices: list = field(default_factory=list)  # [(label, speaker_id)]
    styles: list = field(default_factory=list)
    previewing: int | None = None
    error: str | None = None


@dataclass
class _Start:
    state: str = "idle"  # idle | running | done | error
    stage: str | None = None  # install | wait | daemon_mic
    message: str | None = None
    status_text: str | None = None
    daemon_mic: str | None = None  # checking | ok | silent | unknown


class SetupSession:
    def __init__(self, deps: Deps, cfg: Config, config_path: Path, system: str,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.deps = deps
        self.cfg = cfg
        self.path = config_path
        self.sys = system
        self._clock = clock
        self._lock = threading.RLock()
        # config.toml is read, changed and replaced: two answers saved at once must not race
        self._cfg_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, name="setup-loop",
                                             daemon=True)
        self.engine = _Download()
        self.model = _Download()
        self._prereq_thread: threading.Thread | None = None
        self.login: str = "idle"  # idle | running | ok | limit | login
        self.mic = _Mic()
        self._mic_task: asyncio.Task | None = None
        self.voice = _Voice()
        self._voice_vv: Any = None
        self._voice_stack: contextlib.AsyncExitStack | None = None
        self._voice_task: asyncio.Task | None = None
        self.start = _Start()
        self.paused = False
        self.started = False
        self.finished = threading.Event()
        self.closed = False

    # -- lifecycle --

    def open(self) -> None:
        """Start the loop, pause a running daemon (a re-run) and the downloads."""
        self._loop_thread.start()
        try:
            self.paused = self._run(asyncio.to_thread(wiz.pause_daemon, self.deps.request), 10)
        except Exception:
            log.debug("pausing the daemon failed", exc_info=True)
        self.ensure_prereqs()

    def close(self) -> None:
        """Release the mic and the voice engine; resume the daemon if setup paused it and never
        restarted it. Downloads still running are left to die with the process."""
        if self.closed:
            return
        self.closed = True
        try:
            self._run(self._release(), 30)
        except Exception:
            log.debug("releasing the mic / voice engine failed", exc_info=True)
        if self.paused and not self.started:
            wiz.resume_daemon(self.deps.request)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(5)

    def _run(self, coro: Awaitable[Any], timeout: float) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)  # type: ignore[arg-type]

    def _spawn(self, coro: Awaitable[Any]) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]

    @property
    def downloading(self) -> bool:
        with self._lock:
            return self.engine.state in ("running", "checking", "waiting") or \
                self.model.state in ("running", "checking", "waiting")

    # -- 0. downloads --

    def ensure_prereqs(self) -> None:
        """Fetch what is missing, one after the other (the engine first: the voice step needs
        it); a retry starts again from the item that failed."""
        with self._lock:
            if self._prereq_thread is not None and self._prereq_thread.is_alive():
                return
            for item in (self.engine, self.model):
                if item.state == "error":
                    item.state, item.error = "checking", None
            self._prereq_thread = threading.Thread(target=self._prereqs, name="setup-downloads",
                                                   daemon=True)
            self._prereq_thread.start()

    def _prereqs(self) -> None:
        d = self.deps
        for item, installed, fetch in ((self.engine, d.engine_installed, self._fetch_engine),
                                       (self.model, d.model_installed, self._fetch_model)):
            with self._lock:
                if item.state == "done":
                    continue
            try:
                there = installed()
            except Exception:
                log.debug("install check failed", exc_info=True)
                there = False
            if there:
                with self._lock:
                    item.state = "done"
                continue
            with self._lock:
                item.state, item.done, item.total, item.stage = "running", 0, None, None
                other = self.model if item is self.engine else None
                if other is not None and other.state == "checking":
                    other.state = "waiting"
            try:
                fetch()
            except Exception as e:  # network, disk, checksum: shown with a retry button
                log.warning("download failed", exc_info=True)
                with self._lock:
                    item.state, item.error = "error", str(e) or type(e).__name__
                continue
            with self._lock:
                item.state, item.stage = "done", None

    def _fetch_engine(self) -> None:
        def progress(stage: str, done: int, total: int | None) -> None:
            with self._lock:
                self.engine.stage = stage
                if stage == "download":
                    self.engine.done, self.engine.total = done, total

        self.deps.install_engine(progress)

    def _fetch_model(self) -> None:
        def progress(done: int, total: int | None) -> None:
            with self._lock:
                self.model.done, self.model.total = done, total

        self.deps.download_model(progress)

    # -- 1. Claude login --

    def check_login(self) -> None:
        with self._lock:
            if self.login == "running":
                return
            self.login = "running"
        self._spawn(self._login())

    async def _login(self) -> None:
        try:
            result = await self.deps.login_check(self.cfg.model)
        except Exception:
            log.debug("login check crashed", exc_info=True)
            result = "login"
        with self._lock:
            self.login = result if result in ("ok", "limit") else "login"

    # -- 2. microphone --

    def mic_start(self, index: int | None = None) -> None:
        """(Re)open the level meter; with ``index`` (into the listed choices) that mic is saved
        as ``mic_device`` first."""
        device = self.cfg.mic_device
        if index is not None:
            with self._lock:
                choices = list(self.mic.choices)
            if not 0 <= index < len(choices):
                raise ValueError("no such microphone")
            device = choices[index][0]
            if device != self.cfg.mic_device:
                with self._cfg_lock:
                    update_config({"mic_device": device}, self.path)
                    self.cfg.mic_device = device
        self._run(self._mic_restart(device), 15)

    def mic_stop(self) -> None:
        self._run(self._mic_cancel(), 15)

    async def _mic_cancel(self) -> None:
        task, self._mic_task = self._mic_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        with self._lock:
            self.mic.state, self.mic.level = "idle", 0.0

    async def _mic_restart(self, device: str | int | None) -> None:
        await self._mic_cancel()
        sources = await asyncio.to_thread(self._mic_choices)
        with self._lock:
            self.mic.choices = sources
            self.mic.state, self.mic.level, self.mic.peak = "opening", 0.0, 0.0
            self.mic.fallback = False
        self._mic_task = asyncio.ensure_future(self._meter(device))

    def _mic_choices(self) -> list:
        from zundamon_jarvis.mics import mic_choices

        try:
            sources = self.deps.list_mics()
        except Exception:
            log.debug("listing mics failed", exc_info=True)
            sources = []
        try:
            default = self.deps.default_mic()
        except Exception:
            default = None
        out = []
        for device, label in mic_choices(sources, self.cfg.mic_device):
            if device is None:
                resolved = next((s.label for s in sources if s.name == default), default)
                label = DEFAULT_MIC_LABEL + (f"（{resolved}）" if resolved else "")
            out.append((device, label))
        return out

    async def _meter(self, device: str | int | None) -> None:
        import numpy as np

        audio = self.deps.audio_factory(device=device)
        opening = asyncio.ensure_future(asyncio.to_thread(audio.open))
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:  # left the step while PortAudio was still opening it
            with contextlib.suppress(Exception):
                await opening
            audio.close()
            raise
        except Exception:
            log.debug("mic open failed", exc_info=True)
            with self._lock:
                self.mic.state = "error"
            return
        with self._lock:
            self.mic.state, self.mic.opened_at = "listening", self._clock()
            self.mic.fallback = bool(getattr(audio, "fallback", False))
        try:
            async for frame in audio.frames():
                p = float(np.max(np.abs(frame))) if len(frame) else 0.0
                with self._lock:
                    self.mic.level = max(p, self.mic.level * 0.8)
                    self.mic.peak = max(p, self.mic.peak)
        finally:
            audio.close()

    def _mic_view(self) -> dict:
        m = self.mic
        status = m.state
        if status == "listening":
            status = "ok" if m.peak > 0 else (
                "silent" if self._clock() - m.opened_at >= SILENT_AFTER_S else "listening")
        note = None
        if status == "error":
            note = wiz.mic_problem_note(self.sys, "open")
        elif status == "silent":
            note = wiz.mic_problem_note(self.sys, "silent")
        return {
            "choices": [{"label": label, "selected": device == self.cfg.mic_device}
                        for device, label in m.choices],
            "status": status, "level": round(m.level, 4), "peak": round(m.peak, 4), "note": note,
            "fallback_note": MIC_FALLBACK_NOTE if m.fallback else None,
        }

    # -- 4. voice --

    def voice_open(self) -> None:
        with self._lock:
            if self.voice.state not in ("idle", "down"):
                return
            self.voice.state, self.voice.message, self.voice.error = "waiting", None, None
        self._spawn(self._voice_open())

    async def _voice_open(self) -> None:
        self._voice_task = asyncio.current_task()  # _release cancels it (that stops a starting engine)
        while True:  # the first run: the engine may still be downloading
            with self._lock:
                settled = self.engine.state in ("done", "error")
            if settled or self.closed:
                break
            await asyncio.sleep(0.5)

        def say(text: str = "") -> None:
            with self._lock:
                self.voice.message = text

        with self._lock:
            self.voice.state = "starting"
        stack = contextlib.AsyncExitStack()
        speakers = None
        try:
            vv = await stack.enter_async_context(self.deps.voice_engine(say))
            if vv is not None:
                speakers = await vv.speakers()
        except asyncio.CancelledError:  # setup is closing or starting the services
            await stack.aclose()
            raise
        except Exception:
            log.debug("VOICEVOX failed", exc_info=True)
            vv = None
        if vv is None or speakers is None:
            await stack.aclose()
            with self._lock:
                self.voice.state = "down"
                self.voice.message = ("VOICEVOX につながらないので、試聴できません。"
                                      "声はあとで設定（Settings…）からも選べます。")
            return
        styles = talk_styles(speakers)
        self._voice_vv, self._voice_stack = vv, stack
        with self._lock:
            self.voice.styles = styles
            self.voice.choices = wiz.voice_choices(styles)
            self.voice.state, self.voice.message = "ready", None

    async def _voice_close(self) -> None:
        task, self._voice_task = self._voice_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        stack, self._voice_stack, self._voice_vv = self._voice_stack, None, None
        if stack is not None:
            await stack.aclose()
        with self._lock:
            if self.voice.state in ("waiting", "starting", "ready"):
                self.voice.state, self.voice.message = "idle", None

    def voice_preview(self, speaker_id: int) -> None:
        with self._lock:
            if self.voice.state != "ready":
                raise Refused("声のエンジンの準備ができていません。")
            if self.voice.previewing is not None:
                raise Refused("いま試聴しています。")
            if speaker_id not in [sid for _, sid in self.voice.choices]:
                raise ValueError("no such voice")
            self.voice.previewing, self.voice.error = speaker_id, None
        self._spawn(self._preview(speaker_id))

    async def _preview(self, speaker_id: int) -> None:
        try:
            vv = self._voice_vv
            if vv is None:
                raise RuntimeError("VOICEVOX が閉じています")
            await self.deps.play(await vv.synth(wiz.SAMPLE, speaker_id, speed=self.cfg.speed))
        except Exception as e:
            log.debug("preview failed", exc_info=True)
            with self._lock:
                self.voice.error = f"試し読みできませんでした（{e}）"
        finally:
            with self._lock:
                self.voice.previewing = None

    def voice_select(self, speaker_id: int) -> None:
        with self._lock:
            ids = [sid for _, sid in self.voice.choices]
        if speaker_id not in ids:
            raise ValueError("no such voice")
        with self._cfg_lock:
            wiz.save_voice(speaker_id, self.cfg, self.path)

    def set_speed(self, value: Any) -> None:
        speed = valid_speed(value)
        if speed is None:
            raise ValueError(f"speed must be a number in {SPEED_MIN:g}..{SPEED_MAX:g}")
        with self._cfg_lock:
            update_config({"speed": speed}, self.path)
            self.cfg.speed = speed

    def _voice_view(self) -> dict:
        v = self.voice
        sid = self.cfg.speaker_id
        current = wiz._voice_label(v.styles, sid)
        return {
            "state": v.state, "message": v.message, "error": v.error, "previewing": v.previewing,
            "choices": [{"label": label, "speaker_id": s} for label, s in v.choices],
            "current": sid, "current_label": current,
            "speed": self.cfg.speed, "speed_min": SPEED_MIN, "speed_max": SPEED_MAX,
        }

    # -- 5. knowledge folder --

    def set_knowledge(self, raw: Any) -> None:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            with self._cfg_lock:
                wiz.save_knowledge_dir(None, self.cfg, self.path)
            return
        if not isinstance(raw, str):
            raise ValueError("path must be a string")
        folder = wiz.resolve_folder(raw, Path.home())
        if folder is None:
            raise Refused(f"「{raw.strip()}」というフォルダが見つかりません。")
        with self._cfg_lock:
            wiz.save_knowledge_dir(folder, self.cfg, self.path)

    def pick_folder(self) -> str | None:
        if self.deps.pick_folder is None:
            raise Refused("このパソコンではフォルダを選ぶ画面を出せません。場所を入力してください。")
        return self.deps.pick_folder()

    # -- 6. model --

    def set_model(self, model: Any) -> None:
        if model not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        with self._cfg_lock:
            wiz.save_model(model, self.cfg, self.path)

    # -- 7. start --

    def start_services(self) -> None:
        with self._lock:
            if self.login not in ("ok", "limit"):
                raise Refused("先に Claude へのログインを確かめてください。")
            if self.engine.state != "done" or self.model.state != "done":
                raise Refused("声のエンジンと文字起こしのモデルの準備が終わってから起動できます。")
            if self.start.state == "running":
                return
            self.start = _Start(state="running", stage="install")
        self._spawn(self._start())

    async def _start(self) -> None:
        await self._release()  # the daemon opens the mic and VOICEVOX's port itself
        try:
            await asyncio.to_thread(self.deps.start_services)
        except Exception as e:  # the CLI's START_ERRORS and anything else: say it on the page
            log.warning("starting the services failed", exc_info=True)
            with self._lock:
                self.start.state, self.start.message = "error", wiz.start_failed_message(e)
            return
        self.started = True
        with self._lock:
            self.start.stage = "wait"
        status = await wiz.wait_for_status(self.deps.request, self.deps.sleep)
        if not (status and "text" in status):
            with self._lock:
                self.start.state, self.start.stage, self.start.message = "done", None, START_PENDING
            return
        with self._lock:
            self.start.status_text = str(status["text"])
        if self.sys in wiz.DAEMON_MIC_SYSTEMS:
            await self._daemon_mic()
        with self._lock:
            self.start.state, self.start.stage = "done", None

    async def _daemon_mic(self) -> None:
        with self._lock:
            self.start.stage, self.start.daemon_mic = "daemon_mic", "checking"
        verdict = await wiz.probe_daemon_mic(self.deps.request, self.deps.sleep, self.sys)
        with self._lock:
            self.start.daemon_mic = verdict

    def recheck_daemon_mic(self) -> None:
        with self._lock:
            if (not self.started or self.sys not in wiz.DAEMON_MIC_SYSTEMS
                    or self.start.daemon_mic == "checking"):
                raise Refused("いまは確かめられません。")
            self.start.daemon_mic = "checking"
        self._spawn(self._daemon_mic())

    async def _release(self) -> None:
        await self._mic_cancel()
        await self._voice_close()

    # -- the page's view --

    def _download_view(self, d: _Download) -> dict:
        return {"state": d.state, "stage": d.stage, "done": d.done, "total": d.total, "error": d.error}

    def snapshot(self) -> dict:
        with self._lock:
            daemon_mic = self.start.daemon_mic
            return {
                "system": self.sys,
                "config_path": wiz._tilde(self.path),
                "prereq": {"engine": self._download_view(self.engine),
                           "model": self._download_view(self.model)},
                "login": {"state": self.login,
                          "note": {"login": LOGIN_HELP, "limit": wiz.LIMIT_NOTE}.get(self.login)},
                "mic": self._mic_view(),
                "headphones": wiz.headphones_note(self.sys),
                "voice": self._voice_view(),
                "knowledge": {"value": wiz._tilde(self.cfg.knowledge_dir) if self.cfg.knowledge_dir
                              else None, "can_pick": self.deps.pick_folder is not None},
                "model": {"choices": list(MODELS), "current": self.cfg.model},
                "start": {
                    "state": self.start.state, "stage": self.start.stage,
                    "message": self.start.message, "status_text": self.start.status_text,
                    "daemon_mic": daemon_mic,
                    "daemon_mic_text": (wiz.daemon_mic_text(
                        self.sys, "ask" if daemon_mic == "checking" else daemon_mic)
                        if daemon_mic else None),
                },
                "finished": self.finished.is_set(),
            }


# ---- HTTP: pure request -> response ----

@dataclass
class Request:
    method: str
    target: str  # path and query, as sent
    headers: dict[str, str]  # lower-case names
    body: bytes = b""


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
STATIC = {  # published name -> (package-relative path, content type)
    "setup.css": (("assets", "setup", "setup.css"), "text/css; charset=utf-8"),
    "setup.js": (("assets", "setup", "setup.js"), "text/javascript; charset=utf-8"),
    "icon.png": (("assets", "icon.png"), "image/png"),
}
DENIED_PAGE = ("<!doctype html><meta charset=utf-8><title>zundamon-jarvis</title>"
               "<p>このページは開けません。ターミナルに出ている URL をそのまま開いてください。</p>")


def _asset(*parts: str) -> bytes:
    return resources.files("zundamon_jarvis").joinpath(*parts).read_bytes()


def _json(status: int, data: Any) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _error(status: int, message: str) -> Response:
    return _json(status, {"ok": False, "error": message})


@dataclass
class App:
    session: SetupSession
    token: str
    port: int = 0  # set once the server is bound
    clock: Callable[[], float] = time.monotonic
    last_seen: float | None = None
    bye_at: float | None = None

    def hosts(self) -> tuple[str, ...]:
        return (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def cookie_name(self) -> str:
        return f"{COOKIE}_{self.port}"  # cookies ignore ports: a stale run's cookie must not clash

    def idle_for(self, now: float | None = None) -> float | None:
        """How long the page is overdue: >0 means it is gone (None: never came yet)."""
        now = self.clock() if now is None else now
        if self.last_seen is None:
            return None
        if self.bye_at is not None and self.bye_at >= self.last_seen:
            return now - self.bye_at - BYE_S
        return now - self.last_seen - GONE_S


def _authorized(app: App, headers: dict[str, str]) -> bool:
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except http.cookies.CookieError:
        return False
    morsel = jar.get(app.cookie_name())
    given = morsel.value if morsel else ""
    return hmac.compare_digest(given.encode(), app.token.encode())


def handle(app: App, req: Request) -> Response:
    s = app.session
    if req.headers.get("host", "") not in app.hosts():
        return Response(403, b"bad host", "text/plain; charset=utf-8")
    origin = req.headers.get("origin")
    if origin is not None and origin not in tuple(f"http://{h}" for h in app.hosts()):
        return Response(403, b"bad origin", "text/plain; charset=utf-8")
    url = urllib.parse.urlsplit(req.target)
    path = url.path
    if req.method == "GET" and path == "/":
        token = urllib.parse.parse_qs(url.query).get("t", [""])[0]
        if token:
            if not hmac.compare_digest(token.encode(), app.token.encode()):
                return Response(403, DENIED_PAGE.encode(), "text/html; charset=utf-8")
            # Trade the token for a cookie and drop it from the address bar.
            return Response(303, headers=[
                ("Location", "/"),
                ("Set-Cookie", f"{app.cookie_name()}={app.token}; Path=/; HttpOnly; SameSite=Strict")])
    if not _authorized(app, req.headers):
        if path == "/":
            return Response(403, DENIED_PAGE.encode(), "text/html; charset=utf-8")
        return _error(403, "forbidden")
    app.last_seen = app.clock()
    if req.method == "GET":
        if path == "/":
            return Response(200, _asset("assets", "setup", "index.html"), "text/html; charset=utf-8")
        if path.startswith("/assets/") and path[len("/assets/"):] in STATIC:
            parts, ctype = STATIC[path[len("/assets/"):]]
            return Response(200, _asset(*parts), ctype)
        if path == "/api/state":
            return _json(200, s.snapshot())
        return _error(404, "not found")
    if req.method != "POST":
        return _error(405, "method not allowed")
    ctype = req.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return _error(415, "application/json only")
    if len(req.body) > MAX_BODY:
        return _error(413, "too large")
    try:
        data = json.loads(req.body or b"{}")
    except ValueError:
        return _error(400, "bad json")
    if not isinstance(data, dict):
        return _error(400, "a JSON object please")
    route = ROUTES.get(path)
    if route is None:
        return _error(404, "not found")
    try:
        extra = route(app, data)
    except Refused as e:
        return _error(409, str(e))
    except (ValueError, TypeError) as e:
        return _error(400, str(e))
    except Exception as e:  # a write that failed (disk, a config that broke meanwhile)
        log.warning("%s failed", path, exc_info=True)
        return _error(500, str(e))
    return _json(200, {"ok": True, **(extra or {}), "state": s.snapshot()})


def _int(data: dict, key: str) -> int:
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{key} must be an integer")
    return v


def _bye(app: App, data: dict) -> None:
    app.bye_at = app.clock()


def _finish(app: App, data: dict) -> None:
    app.session.finished.set()


def _number(data: dict, key: str) -> float:
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{key} must be a number")
    return float(v)


ROUTES: dict[str, Callable[[App, dict], dict | None]] = {
    "/api/prereq/retry": lambda app, d: app.session.ensure_prereqs(),
    "/api/login/check": lambda app, d: app.session.check_login(),
    "/api/mic/start": lambda app, d: app.session.mic_start(None if d.get("index") is None
                                                            else _int(d, "index")),
    "/api/mic/stop": lambda app, d: app.session.mic_stop(),
    "/api/voice/open": lambda app, d: app.session.voice_open(),
    "/api/voice/preview": lambda app, d: app.session.voice_preview(_int(d, "speaker_id")),
    "/api/voice/select": lambda app, d: app.session.voice_select(_int(d, "speaker_id")),
    "/api/speed": lambda app, d: app.session.set_speed(_number(d, "speed")),
    "/api/knowledge": lambda app, d: app.session.set_knowledge(d.get("path")),
    "/api/knowledge/pick": lambda app, d: {"path": app.session.pick_folder()},
    "/api/model": lambda app, d: app.session.set_model(d.get("model")),
    "/api/start": lambda app, d: app.session.start_services(),
    "/api/daemon-mic/check": lambda app, d: app.session.recheck_daemon_mic(),
    "/api/bye": _bye,
    "/api/finish": _finish,
}


class _Handler(BaseHTTPRequestHandler):
    app: App  # set on the subclass made by make_server
    server_version = "zundamon-jarvis-setup"
    sys_version = ""

    def _serve(self) -> None:
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            resp = _error(413, "too large")
        else:
            body = self.rfile.read(n) if n else b""
            req = Request(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            try:
                resp = handle(self.app, req)
            except Exception:
                log.exception("setup request failed")
                resp = _error(500, "internal error")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in resp.headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the stdlib's name
        log.debug("setup http: " + format, *args)


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    handler = type("SetupHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    app.port = server.server_address[1]
    return server


# ---- running it ----

def _watch(app: App, server: ThreadingHTTPServer, stop: threading.Event,
           on_stop: Callable[[], None] = lambda: None) -> None:
    """Stop the server once the page finished, or has been gone a while (never during a
    download: a closed tab can be opened again from the terminal's URL meanwhile); then
    ``on_stop`` (closes the app window)."""
    started = app.clock()
    while not stop.wait(1.0):
        s = app.session
        if s.finished.is_set():
            time.sleep(0.5)  # let the finish response reach the page
            break
        idle = app.idle_for()
        if s.downloading:
            continue
        if (idle is not None and idle > 0) or (
                app.last_seen is None and app.clock() - started > FIRST_CONTACT_S):
            log.info("the setup page is gone; stopping")
            break
    server.shutdown()
    if not stop.is_set():
        on_stop()


def _sigterm_as_interrupt() -> Callable[[], None]:
    """A closed terminal or a kill ends setup like Ctrl-C does: the cleanup still runs (the mic,
    the engine started for the samples, resuming a daemon setup paused). Returns the undo."""
    import signal

    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    saved = {}
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            saved[sig] = signal.signal(sig, interrupt)

    def restore() -> None:
        for sig, handler in saved.items():
            signal.signal(sig, handler)

    return restore


def _open_browser(sys_: str, environ: dict | None = None) -> Callable[[str], bool]:
    """``webbrowser.open``, except on a Linux without a display (an SSH session): there it would
    run a console browser (lynx, w3m) in this terminal, which cannot show the page and blocks."""
    import os
    import webbrowser

    env = os.environ if environ is None else environ
    if sys_ == "linux" and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return lambda url: False
    return webbrowser.open


# ---- the app window (macOS, Windows) ----

WINDOW_TITLE = "ずんだもんジャーヴィスの設定"
WINDOW_SIZE = (960, 720)
WINDOW_MIN = (420, 560)


class AppWindow:
    """The setup page in a native window through pywebview (``webview`` is passed in)."""

    def __init__(self, webview: Any) -> None:
        self._wv = webview
        self._win: Any = None

    def run(self, url: str) -> None:
        """Show ``url`` and block (on the main thread: Cocoa needs it) until the window is closed."""
        w, h = WINDOW_SIZE
        self._win = self._wv.create_window(WINDOW_TITLE, url, width=w, height=h, min_size=WINDOW_MIN,
                                           background_color="#121712")
        self._wv.start()

    def close(self) -> None:
        if self._win is not None:
            self._win.destroy()

    def pick_folder(self) -> str | None:
        """The OS's own folder dialog, on top of the setup window."""
        if self._win is None:
            return None
        kinds = getattr(self._wv, "FileDialog", None)  # pywebview 6; FOLDER_DIALOG before
        kind = kinds.FOLDER if kinds is not None else self._wv.FOLDER_DIALOG
        picked = self._win.create_file_dialog(kind, directory=str(Path.home()))
        if not picked:
            return None
        return str(picked[0] if isinstance(picked, (list, tuple)) else picked)


def app_window(sys_: str) -> AppWindow | None:
    """An app window on macOS and Windows; None on Linux (pywebview would need the system's GTK
    WebKit, which the venv cannot see) or when pywebview is not installed."""
    if sys_ not in ("mac", "windows"):
        return None
    try:
        import webview
    except Exception:
        log.debug("pywebview unavailable", exc_info=True)
        return None
    return AppWindow(webview)


_DEFAULT: Any = object()


def run_gui(deps: Deps | None = None, *, config_path: Path | None = None, system: str | None = None,
            open_browser: Callable[[str], bool] | None = None, out: Callable[..., None] = print,
            port: int = 0, window: Any = _DEFAULT) -> int:
    path = config_path or config_mod.CONFIG_PATH
    sys_ = system or platform.system()
    cfg, problem = wiz.read_config(path)
    if cfg is None:
        out(problem)
        return 1
    if window is _DEFAULT:
        window = app_window(sys_)
    deps = deps or default_deps(sys_)
    if window is not None:
        deps.pick_folder = window.pick_folder
    session = SetupSession(deps, cfg, path, sys_)
    app = App(session, secrets.token_urlsafe(24))
    server = make_server(app, port)
    stop = threading.Event()
    on_stop = window.close if window is not None else (lambda: None)
    watcher = threading.Thread(target=_watch, args=(app, server, stop, on_stop), name="setup-watch",
                               daemon=True)
    restore_signals = _sigterm_as_interrupt()
    try:
        session.open()
        watcher.start()
        if window is not None and _run_window(window, app, server, out):
            pass  # the window was closed (or setup finished and closed it)
        else:
            if window is not None:
                deps.pick_folder = tk_folder_picker()  # the window's own dialog is gone with it
            out("設定の画面をブラウザで開きます。開かないときは、次の URL をブラウザに貼り付けてください:")
            out(f"  {app.url()}")
            out("（ブラウザが使えないときは Ctrl-C で止めて `zundamon-jarvis setup --cli` を実行してください）")
            if open_browser is None:
                open_browser = _open_browser(sys_)
            try:
                if not open_browser(app.url()):
                    out("ブラウザを開けませんでした。上の URL を開いてください。")
            except Exception:
                log.debug("webbrowser failed", exc_info=True)
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        out("\n中断しました。")
    finally:
        stop.set()
        server.server_close()
        session.close()
        restore_signals()
    if session.started:
        out(f"設定が終わりました。設定ファイル: {wiz._tilde(path)}")
        return 0
    out("設定は途中です。もう一度 `zundamon-jarvis setup` を実行すると、続きから設定できます。")
    return 1


def _run_window(window: AppWindow, app: App, server: ThreadingHTTPServer,
                out: Callable[..., None]) -> bool:
    """Serve on a thread and show the app window on this (the main) thread until it closes.
    False when no window could be shown (no WebView2, say): the caller falls back to the browser."""
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                               name="setup-http", daemon=True)
    serving.start()
    out("設定の画面を開きます（閉じると設定を終えます）。")
    try:
        window.run(app.url())
    except Exception:
        log.warning("the setup window could not be shown; using the browser", exc_info=True)
        server.shutdown()
        serving.join()
        return False
    server.shutdown()  # closed by the user: setup ends here (finished or not)
    serving.join()
    return True


def main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis setup [--cli | --demo]` (dispatched by zundamon_jarvis.cli)."""
    args = list(argv or [])
    if args == ["--cli"]:
        return wiz.main([])
    if args == ["--demo"]:
        from zundamon_jarvis.setup_demo import run_demo

        return run_demo()
    if args:
        print("usage: zundamon-jarvis setup [--cli]", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return run_gui()
