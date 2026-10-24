"""`zundamon-jarvisctl settings`: a small keyboard-driven settings window (Textual).

Opened by right-clicking the bar module (``zundamon-jarvisctl window settings``: a floating terminal of
class ``zundamon-jarvis-settings``). Rows, ↑↓ to move, ←→ to change, Enter to act, Esc / q to close:

- Voice speed: 0.8..2.0 in 0.1 steps. Applied live; a short sample is spoken at the new speed
  (after a short pause in the key presses, so holding → doesn't flood the speaker).
- Voice: every VOICEVOX speaking style, grouped by speaker (the engine's ``/speakers``). Moving
  through the list plays a sample in that voice; Enter picks it, Esc goes back unchanged.
- Model: Sonnet / Opus / Haiku for the next Claude session. Switch now closes the current
  session if it is idle, so the next utterance starts on the new model.
- Mic: the OS default input or one of the input devices (``pactl``). Saved to config.toml; the
  open stream keeps its device, so it applies when ZundamonJarvis restarts.
- Pause / Resume: the same toggle as SUPER+ALT+J.

With the daemon running, every change goes through the control socket (the daemon applies it
and writes config.toml). Without it the window writes config.toml itself (``update_config``:
only that key) and says the change applies on the next start; samples need the daemon.

All the blocking work (socket, VOICEVOX) runs on one worker thread, in order. The pure helpers
(``step_speed``, ``voice_groups``, ``voice_label``, ``cycle_model``, ``model_note``, ``mic_note``,
``View``; the device list logic is in ``zundamon_jarvis.mics``)
hold the logic and are tested without a terminal.
"""

import asyncio
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from zundamon_jarvis.config import (
    CONFIG_PATH,
    DEFAULT_MODEL,
    DEFAULT_SPEED,
    MODELS,
    SPEED_MAX,
    SPEED_MIN,
    Config,
    load_config,
    update_config,
    valid_mic_device,
)
from zundamon_jarvis.control import request as control_request
from zundamon_jarvis.mics import (
    MicSource,
    cycle_mic,
    default_source_name,
    list_sources,
    mic_label,
    mic_value,
    parse_mic,
)
from zundamon_jarvis.live_settings import parse_model, parse_speaker, parse_speed
from zundamon_jarvis.tts import DEFAULT_SPEAKER, DEFAULT_URL, talk_styles

SPEED_STEP = 0.1
SPEED_SAMPLE = "This is how fast I will talk."
VOICE_SAMPLE = "This is the voice I will use."
PREVIEW_DELAY_S = 0.45
POLL_S = 2.0
MODEL_LABEL = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
STATE_LABEL = {"listening": "Listening", "paused": "Paused", "thinking": "Thinking",
               "speaking": "Speaking", "limit": "Limit", "offline": "Offline"}
OFFLINE_NOTE = "applies on next start"
STOPPED = "ZundamonJarvis is stopped — " + OFFLINE_NOTE
LABEL_CELLS = 17


# ---- pure logic ----

def step_speed(speed: float, delta: int) -> float:
    """``speed`` moved by ``delta`` tenths, on the 0.1 grid, clamped to 0.8..2.0."""
    tenths = math.floor(speed * 10 + 0.5 + 1e-9) + delta
    return min(max(tenths / 10, SPEED_MIN), SPEED_MAX)


@dataclass(frozen=True)
class Voice:
    speaker: str
    style: str
    id: int


@dataclass
class VoiceGroup:
    name: str
    voices: list[Voice] = field(default_factory=list)


def voice_groups(speakers: Any) -> list[VoiceGroup]:
    groups: dict[str, VoiceGroup] = {}
    for speaker, style, sid in talk_styles(speakers):
        groups.setdefault(speaker, VoiceGroup(speaker)).voices.append(Voice(speaker, style, sid))
    return list(groups.values())


def voice_label(groups: list[VoiceGroup], speaker_id: int | None) -> str:
    if speaker_id is None:
        return f"{DEFAULT_SPEAKER} / ノーマル (default)"
    for g in groups:
        for v in g.voices:
            if v.id == speaker_id:
                return f"{v.speaker} / {v.style}"
    return f"ID {speaker_id}"


def cycle_model(model: str | None, delta: int) -> str:
    if model not in MODELS:
        return MODELS[0]
    return MODELS[(MODELS.index(model) + delta) % len(MODELS)]


def model_label(model: str | None) -> str:
    return MODEL_LABEL.get(model or "", model or "CLI default")


@dataclass
class View:
    online: bool
    state: str = "off"
    state_text: str = "Stopped"
    speed: float = DEFAULT_SPEED
    speaker_id: int | None = None
    model: str | None = DEFAULT_MODEL
    session_model: str | None = None
    mic_device: str | int | None = None
    mic_active: str | int | None = None

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @classmethod
    def from_reply(cls, resp: dict) -> "View":
        s = resp.get("settings")
        s = s if isinstance(s, dict) else {}
        speed, sid = s.get("speed"), s.get("speaker_id")
        mic = valid_mic_device(s.get("mic_device"))

        def text(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        return cls(online=True, state=str(resp.get("state", "")),
                   state_text=str(resp.get("text", "")),
                   speed=float(speed) if isinstance(speed, (int, float)) else DEFAULT_SPEED,
                   speaker_id=sid if isinstance(sid, int) and not isinstance(sid, bool) else None,
                   model=text(s.get("model")), session_model=text(s.get("session_model")),
                   mic_device=mic, mic_active=valid_mic_device(s.get("mic_active")))

    @classmethod
    def from_config(cls, cfg: Config) -> "View":
        return cls(online=False, speed=cfg.speed, speaker_id=cfg.speaker_id, model=cfg.model,
                   mic_device=cfg.mic_device, mic_active=cfg.mic_device)


def model_note(view: View) -> str:
    if not view.online:
        return OFFLINE_NOTE
    if view.session_model is None:
        return "applies from the next conversation"
    if view.session_model != view.model:
        return f"This conversation stays on {model_label(view.session_model)} — use \"Switch now\" to apply"
    return ""


BLUETOOTH_MIC_WARNING = ("Bluetooth headset mics switch the headset to call quality "
                         "(music sounds worse while the mic is in use)")


def mic_note(view: View, default_name: str | None = None) -> str:
    """Hint under the Mic row. ``default_name`` is the OS default source (for "System default")."""
    if not view.online:
        note = OFFLINE_NOTE
    elif view.mic_device != view.mic_active:
        note = "Restart ZundamonJarvis to apply"
    else:
        note = "←→ to switch (applies when ZundamonJarvis restarts)"
    chosen = default_name if view.mic_device is None else view.mic_device
    if isinstance(chosen, str) and chosen.startswith("bluez_input."):
        note += f"\n⚠ {BLUETOOTH_MIC_WARNING}"
    return note


# ---- talking to the daemon (or config.toml when it is not running) ----

class Client(Protocol):
    def load(self) -> View: ...
    def speakers(self) -> list[dict]: ...
    def mics(self) -> list[MicSource]: ...
    def default_mic(self) -> str | None: ...
    def set(self, key: str, value: Any) -> tuple[View, str | None]: ...
    def preview(self, speaker_id: int, speed: float, text: str) -> str | None: ...
    def toggle(self) -> tuple[View, str | None]: ...
    def new_session(self) -> tuple[View, str | None]: ...


_BUSY = {"user_speaking": "You are speaking, so no preview",
         "speaking": "ZundamonJarvis is speaking, so no preview"}
_CONFIG_KEY = {"speed": "speed", "speaker": "speaker_id", "model": "model", "mic": "mic_device"}
_PARSE: dict[str, Callable[[str], Any]] = {"speed": parse_speed, "speaker": parse_speaker,
                                           "model": parse_model, "mic": parse_mic}


class SettingsClient:
    def __init__(
        self,
        request: Callable[..., dict] = control_request,
        config_path: Path = CONFIG_PATH,
        voicevox_url: str = DEFAULT_URL,
    ) -> None:
        self._request = request
        self._path = config_path
        self._vv_url = voicevox_url

    def _ask(self, cmd: str, timeout: float = 3.0) -> dict | None:
        """The daemon's reply, or None if it is not running."""
        try:
            resp = self._request(cmd, timeout=timeout)
        except (OSError, ValueError):
            return None
        return resp if isinstance(resp, dict) else None

    def _offline(self) -> View:
        try:
            return View.from_config(load_config(self._path))
        except (OSError, ValueError):
            return View(online=False)

    def load(self) -> View:
        resp = self._ask("get")
        if resp is None or "error" in resp:
            return self._offline()
        return View.from_reply(resp)

    def speakers(self) -> list[dict]:
        try:
            r = httpx.get(f"{self._vv_url}/speakers", timeout=3)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def mics(self) -> list[MicSource]:
        return list_sources()

    def default_mic(self) -> str | None:
        return default_source_name()

    def set(self, key: str, value: Any) -> tuple[View, str | None]:
        resp = self._ask(f"set {key} {value}", timeout=6.0)
        if resp is not None:
            if "error" in resp:
                return self.load(), f"Cannot change: {resp['error']}"
            msg = None if resp.get("saved", True) else "Applied, but could not save to config.toml"
            return View.from_reply(resp), msg
        try:
            parsed = _PARSE[key](str(value))
            update_config({_CONFIG_KEY[key]: parsed}, self._path)
        except (KeyError, ValueError, OSError) as e:
            return self._offline(), f"Cannot change: {e}"
        return self._offline(), STOPPED

    def preview(self, speaker_id: int, speed: float, text: str) -> str | None:
        resp = self._ask(f"preview {speaker_id} {speed} {text}", timeout=12.0)
        if resp is None:
            return "ZundamonJarvis is not running, so no preview"
        if "error" in resp:
            return f"Cannot preview: {resp['error']}"
        if resp.get("ok"):
            return None
        return _BUSY.get(resp.get("busy", ""))

    def toggle(self) -> tuple[View, str | None]:
        resp = self._ask("toggle")
        if resp is None:
            return self._offline(), "ZundamonJarvis is not running"
        return self.load(), None

    def new_session(self) -> tuple[View, str | None]:
        resp = self._ask("newsession")
        if resp is None:
            return self._offline(), f"ZundamonJarvis is not running ({OFFLINE_NOTE})"
        if "error" in resp:
            return self.load(), resp["error"]
        view = View.from_reply(resp)
        model = model_label(view.model)
        return view, {
            "closed": f"Conversation reset. From the next utterance: {model}",
            "none": f"No conversation yet. From the next utterance: {model}",
            "busy": "Busy right now, cannot switch (applies from the next conversation)",
        }.get(resp.get("session", ""), None)


# ---- the window ----

def _row(label: str, value: str) -> Text:
    pad = " " * max(LABEL_CELLS - cell_len(label), 1)
    return Text(f"{label}{pad}{value}")


ROWS = ("speed", "voice", "model", "mic", "switch", "pause")
HINT = {
    "speed": f"←→ to adjust ({SPEED_MIN:.1f}–{SPEED_MAX:.1f}). Plays a sample on every change",
    "voice": "Enter for the list (plays a sample as you move)",
    "switch": "Reset the conversation; the next utterance uses the new model",
    "pause": "Enter to toggle (SUPER+ALT+J works too)",
}

CSS = """
Screen { padding: 0 1; }
#top { height: 1; margin-bottom: 1; }
#title { width: 1fr; text-style: bold; color: $accent; }
#status { width: auto; color: $text-muted; }
OptionList, OptionList:focus { border: none; padding: 0; background: transparent; }
#rows { height: auto; }
#voices { height: 1fr; }
#hint { height: auto; margin-top: 1; color: $text-muted; }
#msg { height: auto; color: $warning; }
#keys { dock: bottom; height: 1; color: $text-muted; }
"""


class VoiceScreen(Screen):
    """Every voice, grouped by speaker; moving previews, Enter picks, Esc goes back."""

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q", "back", "Back", show=False)]

    def __init__(self, groups: list[VoiceGroup], current: int | None,
                 on_preview: Callable[[int], None]) -> None:
        super().__init__()
        self._groups = groups
        self._current = current
        self._last = current  # the voice highlighted without being previewed (at open)
        self._on_preview = on_preview

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("Choose a voice", id="title")
            yield Static(voice_label(self._groups, self._current), id="status")
        options: list[Option] = []
        for g in self._groups:
            options.append(Option(Text(g.name, style="bold"), disabled=True))
            for v in g.voices:
                options.append(Option(Text(f"   {v.style}"), id=f"v{v.id}"))
        yield OptionList(*options, id="voices")
        yield Static("", id="msg")
        yield Static("↑↓ Preview  Enter Select  Esc Back", id="keys")

    def on_mount(self) -> None:
        voices = self.query_one("#voices", OptionList)
        if self._current is not None:
            try:
                voices.highlighted = voices.get_option_index(f"v{self._current}")
            except Exception:
                pass
        voices.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        sid = _voice_id(event.option)
        if sid is None or sid == self._last:
            return
        self._last = sid
        self._on_preview(sid)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        sid = _voice_id(event.option)
        if sid is not None:
            self.dismiss(sid)

    def action_back(self) -> None:
        self.dismiss(None)


def _voice_id(option: Option) -> int | None:
    oid = option.id or ""
    return int(oid[1:]) if oid.startswith("v") and oid[1:].isdigit() else None


class SettingsApp(App):
    TITLE = "ZundamonJarvis Settings"
    CSS = CSS
    BINDINGS = [
        Binding("escape", "quit", "Close"),
        Binding("q", "quit", "Close", show=False),
        Binding("left", "adjust(-1)", "Change", show=False),
        Binding("right", "adjust(1)", "Change", show=False),
    ]

    def __init__(self, client: Client, *, preview_delay: float = PREVIEW_DELAY_S,
                 poll_s: float = POLL_S) -> None:
        super().__init__(ansi_color=True)  # the terminal's (Omarchy theme's) own colors
        self._client = client
        self._preview_delay = preview_delay
        self._poll_s = poll_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zundamon-jarvis-settings")
        self.view = View(online=False)
        self._groups: list[VoiceGroup] = []
        self._mics: list[MicSource] = []
        self._default_mic: str | None = None
        self._pending = 0  # changes sent, reply not back yet: polls don't overwrite them
        self._preview_timer: Timer | None = None
        self._message = ""

    # ---- layout ----

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("ZundamonJarvis Settings", id="title")
            yield Static("", id="status")
        yield OptionList(*(Option(Text(""), id=r) for r in ROWS), id="rows")
        yield Static("", id="hint")
        yield Static("", id="msg")
        yield Static("↑↓ Select  ←→ Change  Enter Confirm  Esc Close", id="keys")

    @property
    def _main(self) -> Screen:
        """The settings rows' screen (the bottom of the stack), whichever screen is on top."""
        return self.screen_stack[0]

    async def on_mount(self) -> None:
        rows = self._main.query_one("#rows", OptionList)
        rows.highlighted = 0
        rows.focus()
        self.view = await self._call(self._client.load)
        self._render_all()
        self.run_worker(self._load_voices(), group="voices")
        self.run_worker(self._load_mics(), group="mics")
        if self._poll_s > 0:
            self.set_interval(self._poll_s, self._poll)

    async def on_unmount(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def _load_voices(self) -> None:
        self._groups = voice_groups(await self._call(self._client.speakers))
        self._render_all()

    async def _load_mics(self) -> None:
        self._mics = await self._call(self._client.mics)
        self._default_mic = await self._call(self._client.default_mic)
        self._render_all()

    async def _poll(self) -> None:
        if self._pending:
            return
        view = await self._call(self._client.load)
        if not self._pending:
            self.view = view
            self._render_all()

    def _render_all(self) -> None:
        v = self.view
        if v.online:
            status = f"● {STATE_LABEL.get(v.state, v.state_text or v.state)}"
        else:
            status = f"○ Stopped — {OFFLINE_NOTE}"
        self._main.query_one("#status", Static).update(status)
        rows = self._main.query_one("#rows", OptionList)
        values = {
            "speed": _row("Voice speed", f"◂ {v.speed:.1f} ▸"),
            "voice": _row("Voice", f"{voice_label(self._groups, v.speaker_id)}  ›"),
            "model": _row("Model", f"◂ {model_label(v.model)} ▸"),
            "mic": _row("Mic", f"◂ {mic_label(self._mics, v.mic_device, self._default_mic)} ▸"),
            "switch": _row("Switch now", "Reset conversation"),
            "pause": _row("Pause / Resume",
                          "⏸ Paused — Enter to resume" if v.paused else "▶ Listening"
                          if v.online else "—"),
        }
        for rid, prompt in values.items():
            rows.replace_option_prompt(rid, prompt)
        self._render_hint()

    def _render_hint(self) -> None:
        row = self._row()
        if row == "model":
            hint = model_note(self.view) or "←→ to switch"
        elif row == "mic":
            hint = mic_note(self.view, self._default_mic)
        else:
            hint = HINT.get(row or "", "")
        self._main.query_one("#hint", Static).update(hint)
        self._main.query_one("#msg", Static).update(self._message)

    def _row(self) -> str | None:
        rows = self._main.query_one("#rows", OptionList)
        opt = rows.highlighted_option
        return opt.id if opt is not None else None

    def _say(self, message: str | None) -> None:
        self._message = message or ""
        self._main.query_one("#msg", Static).update(self._message)
        if isinstance(self.screen, VoiceScreen):
            self.screen.query_one("#msg", Static).update(self._message)

    # ---- actions ----

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "rows":
            self._render_hint()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "rows":
            return
        row = event.option.id
        if row == "voice":
            await self._open_voices()
        elif row == "pause":
            self.view, msg = await self._call(self._client.toggle)
            self._render_all()
            self._say(msg)
        elif row == "switch":
            self.view, msg = await self._call(self._client.new_session)
            self._render_all()
            self._say(msg)

    async def action_adjust(self, delta: int) -> None:
        if isinstance(self.screen, VoiceScreen):
            return
        row = self._row()
        if row == "speed":
            speed = step_speed(self.view.speed, delta)
            if speed == self.view.speed:
                return
            self.view.speed = speed
            self._render_all()
            await self._set("speed", speed)
            self._schedule_preview(self.view.speaker_id, SPEED_SAMPLE)
        elif row == "model":
            self.view.model = cycle_model(self.view.model, delta)
            self._render_all()
            await self._set("model", self.view.model)
        elif row == "mic":
            mic = cycle_mic(self._mics, self.view.mic_device, delta)
            if mic == self.view.mic_device:
                return
            self.view.mic_device = mic
            self._render_all()
            await self._set("mic", mic_value(mic))

    async def _set(self, key: str, value: Any) -> None:
        self._pending += 1
        try:
            view, msg = await self._call(self._client.set, key, value)
        finally:
            self._pending -= 1
        if not self._pending:
            self.view = view
            self._render_all()
        self._say(msg)

    async def _open_voices(self) -> None:
        if not self._groups:
            await self._load_voices()
        if not self._groups:
            self._say("Cannot reach VOICEVOX, so the voice list is unavailable")
            return

        def picked(sid: int | None) -> None:
            if self._preview_timer is not None:
                self._preview_timer.stop()
            if sid is not None and sid != self.view.speaker_id:
                self.view.speaker_id = sid
                self._render_all()
                self.run_worker(self._set("speaker", sid), group="set")

        self.push_screen(
            VoiceScreen(self._groups, self.view.speaker_id,
                        lambda sid: self._schedule_preview(sid, VOICE_SAMPLE)),
            picked,
        )

    # ---- previews ----

    def _schedule_preview(self, speaker_id: int | None, text: str) -> None:
        if not self.view.online or speaker_id is None:
            if not self.view.online:
                self._say(STOPPED)
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        speed = self.view.speed
        self._preview_timer = self.set_timer(
            self._preview_delay,
            lambda: self.run_worker(self._preview(speaker_id, speed, text), group="preview"),
        )

    async def _preview(self, speaker_id: int, speed: float, text: str) -> None:
        msg = await self._call(self._client.preview, speaker_id, speed, text)
        if msg:
            self._say(msg)


def main() -> int:
    SettingsApp(SettingsClient()).run()
    return 0
