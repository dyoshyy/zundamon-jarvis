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
- Language: 日本語 / English, the display language of this window, the menus and the setup
  (``zundamon_jarvis.i18n``; saved to config.toml). This window redraws in it at once; the
  menu bar / tray and the conversation window follow the next time they draw.

Every label is in the display language, read from config.toml on each load (and poll), so a
change made elsewhere (the setup page) shows within one poll. The voice samples follow it too.

With the daemon running, every change goes through the control socket (the daemon applies it
and writes config.toml). Without it the window writes config.toml itself (``update_config``:
only that key) and says the change applies on the next start; samples need the daemon.

All the blocking work (socket, VOICEVOX) runs on one worker thread, in order. The pure helpers
(``step_speed``, ``voice_groups``, ``voice_label``, ``cycle_model``, ``model_note``, ``mic_note``,
``View``; the device list logic is in ``zundamon_jarvis.mics``; they take ``lang``, default the
display language)
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

from zundamon_jarvis import i18n
from zundamon_jarvis.config import (
    CONFIG_PATH,
    LANGUAGES,
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
from zundamon_jarvis.control import status_label
from zundamon_jarvis.mics import (
    MicSource,
    cycle_mic,
    default_source_name,
    list_sources,
    mic_label,
    mic_value,
    parse_mic,
)
from zundamon_jarvis.live_settings import parse_language, parse_model, parse_speaker, parse_speed
from zundamon_jarvis.tts import DEFAULT_SPEAKER, DEFAULT_URL, talk_styles

SPEED_STEP = 0.1
PREVIEW_DELAY_S = 0.45
POLL_S = 2.0
MODEL_LABEL = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
LANGUAGE_LABEL = {"ja": "日本語", "en": "English"}  # each in its own language
LABEL_CELLS = 17

TEXT = {
    "title": {"ja": "ZundamonJarvis 設定", "en": "ZundamonJarvis Settings"},
    "keys": {"ja": "↑↓ 選択  ←→ 変更  Enter 決定  Esc 閉じる", "en": "↑↓ Select  ←→ Change  Enter Confirm  Esc Close"},
    "voice.title": {"ja": "声を選ぶ", "en": "Choose a voice"},
    "voice.keys": {"ja": "↑↓ 試聴  Enter 決定  Esc 戻る", "en": "↑↓ Preview  Enter Select  Esc Back"},
    "row.speed": {"ja": "話す速さ", "en": "Voice speed"},
    "row.voice": {"ja": "声", "en": "Voice"},
    "row.model": {"ja": "モデル", "en": "Model"},
    "row.mic": {"ja": "マイク", "en": "Mic"},
    "row.switch": {"ja": "今すぐ切り替え", "en": "Switch now"},
    "row.pause": {"ja": "一時停止 / 再開", "en": "Pause / Resume"},
    "row.language": {"ja": "表示言語", "en": "Language"},
    "switch.value": {"ja": "会話をリセット", "en": "Reset conversation"},
    "pause.paused": {"ja": "⏸ 一時停止中 — Enter で再開", "en": "⏸ Paused — Enter to resume"},
    "pause.listening": {"ja": "▶ 聞き取り中", "en": "▶ Listening"},
    "status.stopped": {"ja": "○ 停止中 — {note}", "en": "○ Stopped — {note}"},
    "offline_note": {"ja": "次の起動から反映", "en": "applies on next start"},
    "stopped": {"ja": "ZundamonJarvis は停止中です — 次の起動から反映", "en": "ZundamonJarvis is stopped — applies on next start"},
    "voice.default": {"ja": "{speaker} / ノーマル（既定）", "en": "{speaker} / ノーマル (default)"},
    "model.cli_default": {"ja": "CLI の既定", "en": "CLI default"},
    "model.next": {"ja": "次の会話から反映", "en": "applies from the next conversation"},
    "model.stays": {"ja": "この会話は {model} のまま — 「今すぐ切り替え」で反映",
                    "en": "This conversation stays on {model} — use \"Switch now\" to apply"},
    "mic.restart": {"ja": "反映するには ZundamonJarvis を再起動してください", "en": "Restart ZundamonJarvis to apply"},
    "mic.switch": {"ja": "←→ で切り替え（ZundamonJarvis の再起動で反映）",
                   "en": "←→ to switch (applies when ZundamonJarvis restarts)"},
    "mic.bluetooth": {
        "ja": "Bluetooth ヘッドセットのマイクを使うと、ヘッドセットが通話用の音質に切り替わります（マイクを使っている間は音楽の音が悪くなります）",
        "en": "Bluetooth headset mics switch the headset to call quality (music sounds worse while the mic is in use)",
    },
    "hint.speed": {"ja": "←→ で調整（{min}–{max}）。変えるたびに試聴します",
                   "en": "←→ to adjust ({min}–{max}). Plays a sample on every change"},
    "hint.voice": {"ja": "Enter で一覧（選ぶと試聴します）", "en": "Enter for the list (plays a sample as you move)"},
    "hint.model": {"ja": "←→ で切り替え", "en": "←→ to switch"},
    "hint.switch": {"ja": "会話をリセットし、次の発話から新しいモデルを使います",
                    "en": "Reset the conversation; the next utterance uses the new model"},
    "hint.pause": {"ja": "Enter で切り替え（SUPER+ALT+J でも）", "en": "Enter to toggle (SUPER+ALT+J works too)"},
    "hint.language": {"ja": "←→ で切り替え（メニューや会話ウィンドウにも反映されます）",
                      "en": "←→ to switch (the menus and the conversation window follow)"},
    "busy.user_speaking": {"ja": "話している最中なので試聴できません", "en": "You are speaking, so no preview"},
    "busy.speaking": {"ja": "ZundamonJarvis が話しているので試聴できません", "en": "ZundamonJarvis is speaking, so no preview"},
    "cannot_change": {"ja": "変更できません: {error}", "en": "Cannot change: {error}"},
    "not_saved": {"ja": "反映しましたが、config.toml に保存できませんでした", "en": "Applied, but could not save to config.toml"},
    "no_preview": {"ja": "ZundamonJarvis が起動していないので試聴できません", "en": "ZundamonJarvis is not running, so no preview"},
    "cannot_preview": {"ja": "試聴できません: {error}", "en": "Cannot preview: {error}"},
    "not_running": {"ja": "ZundamonJarvis が起動していません", "en": "ZundamonJarvis is not running"},
    "not_running_note": {"ja": "ZundamonJarvis が起動していません（{note}）", "en": "ZundamonJarvis is not running ({note})"},
    "session.closed": {"ja": "会話をリセットしました。次の発話から {model} を使います",
                       "en": "Conversation reset. From the next utterance: {model}"},
    "session.none": {"ja": "まだ会話はありません。次の発話から {model} を使います",
                     "en": "No conversation yet. From the next utterance: {model}"},
    "session.busy": {"ja": "応答中のため今は切り替えられません（次の会話から反映）",
                     "en": "Busy right now, cannot switch (applies from the next conversation)"},
    "no_voicevox": {"ja": "VOICEVOX に接続できないため、声の一覧を出せません",
                    "en": "Cannot reach VOICEVOX, so the voice list is unavailable"},
    # spoken by the voice being tried, in the window's language
    "sample.speed": {"ja": "このくらいの速さで話すのだ。", "en": "This is how fast I will talk."},
    "sample.voice": {"ja": "この声で話すのだ。", "en": "This is the voice I will use."},
}


def _t(key: str, lang: str | None = None, **fields: object) -> str:
    return i18n.text(TEXT, key, lang, **fields)


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


def voice_label(groups: list[VoiceGroup], speaker_id: int | None, lang: str | None = None) -> str:
    if speaker_id is None:
        return _t("voice.default", lang, speaker=DEFAULT_SPEAKER)
    for g in groups:
        for v in g.voices:
            if v.id == speaker_id:
                return f"{v.speaker} / {v.style}"
    return f"ID {speaker_id}"


def cycle_model(model: str | None, delta: int) -> str:
    if model not in MODELS:
        return MODELS[0]
    return MODELS[(MODELS.index(model) + delta) % len(MODELS)]


def model_label(model: str | None, lang: str | None = None) -> str:
    return MODEL_LABEL.get(model or "", model or _t("model.cli_default", lang))


def other_language(lang: str, delta: int) -> str:
    """The language ←→ moves to (two of them: either way is the other one)."""
    i = LANGUAGES.index(lang) if lang in LANGUAGES else 0
    return LANGUAGES[(i + delta) % len(LANGUAGES)]


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
    reasons: tuple[str, ...] = ()  # why it is offline (the status's "reasons")
    lang: str | None = None  # the display language when loaded; None: i18n.language()

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

        reasons = resp.get("reasons")
        return cls(online=True, state=str(resp.get("state", "")),
                   state_text=str(resp.get("text", "")),
                   reasons=tuple(r for r in reasons if isinstance(r, str)) if isinstance(reasons, list) else (),
                   speed=float(speed) if isinstance(speed, (int, float)) else DEFAULT_SPEED,
                   speaker_id=sid if isinstance(sid, int) and not isinstance(sid, bool) else None,
                   model=text(s.get("model")), session_model=text(s.get("session_model")),
                   mic_device=mic, mic_active=valid_mic_device(s.get("mic_active")))

    @classmethod
    def from_config(cls, cfg: Config) -> "View":
        return cls(online=False, speed=cfg.speed, speaker_id=cfg.speaker_id, model=cfg.model,
                   mic_device=cfg.mic_device, mic_active=cfg.mic_device)


def model_note(view: View, lang: str | None = None) -> str:
    if not view.online:
        return _t("offline_note", lang)
    if view.session_model is None:
        return _t("model.next", lang)
    if view.session_model != view.model:
        return _t("model.stays", lang, model=model_label(view.session_model, lang))
    return ""


def mic_note(view: View, default_name: str | None = None, lang: str | None = None) -> str:
    """Hint under the Mic row. ``default_name`` is the OS default source (for "System default")."""
    if not view.online:
        note = _t("offline_note", lang)
    elif view.mic_device != view.mic_active:
        note = _t("mic.restart", lang)
    else:
        note = _t("mic.switch", lang)
    chosen = default_name if view.mic_device is None else view.mic_device
    if isinstance(chosen, str) and chosen.startswith("bluez_input."):
        note += f"\n⚠ {_t('mic.bluetooth', lang)}"
    return note


def mic_text(sources: list[MicSource], device: str | int | None, default_name: str | None,
             lang: str | None = None) -> str:
    """``mics.mic_label`` in the display language."""
    return mic_label(sources, device, default_name, lang)


# ---- talking to the daemon (or config.toml when it is not running) ----

class Client(Protocol):
    def load(self) -> View: ...
    def language(self) -> str: ...
    def speakers(self) -> list[dict]: ...
    def mics(self) -> list[MicSource]: ...
    def default_mic(self) -> str | None: ...
    def set(self, key: str, value: Any) -> tuple[View, str | None]: ...
    def preview(self, speaker_id: int, speed: float, text: str) -> str | None: ...
    def toggle(self) -> tuple[View, str | None]: ...
    def new_session(self) -> tuple[View, str | None]: ...


_CONFIG_KEY = {"speed": "speed", "speaker": "speaker_id", "model": "model", "mic": "mic_device",
               "language": "language"}
_PARSE: dict[str, Callable[[str], Any]] = {"speed": parse_speed, "speaker": parse_speaker,
                                           "model": parse_model, "mic": parse_mic,
                                           "language": parse_language}


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

    def language(self) -> str:
        """The display language now (config.toml's, else the OS's)."""
        return i18n.language(self._path)

    def _offline(self) -> View:
        try:
            view = View.from_config(load_config(self._path))
        except (OSError, ValueError):
            view = View(online=False)
        view.lang = self.language()
        return view

    def _online(self, resp: dict) -> View:
        view = View.from_reply(resp)
        view.lang = self.language()
        return view

    def load(self) -> View:
        resp = self._ask("get")
        if resp is None or "error" in resp:
            return self._offline()
        return self._online(resp)

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
                view = self.load()
                return view, _t("cannot_change", view.lang, error=resp["error"])
            view = self._online(resp)
            return view, None if resp.get("saved", True) else _t("not_saved", view.lang)
        try:
            parsed = _PARSE[key](str(value))
            update_config({_CONFIG_KEY[key]: parsed}, self._path)
        except (KeyError, ValueError, OSError) as e:
            view = self._offline()
            return view, _t("cannot_change", view.lang, error=e)
        view = self._offline()
        # the language shows at once (this window, the menus); the rest waits for the daemon
        return view, None if key == "language" else _t("stopped", view.lang)

    def preview(self, speaker_id: int, speed: float, text: str) -> str | None:
        resp = self._ask(f"preview {speaker_id} {speed} {text}", timeout=12.0)
        lang = self.language()
        if resp is None:
            return _t("no_preview", lang)
        if "error" in resp:
            return _t("cannot_preview", lang, error=resp["error"])
        if resp.get("ok"):
            return None
        busy = resp.get("busy", "")
        return _t(f"busy.{busy}", lang) if f"busy.{busy}" in TEXT else None

    def toggle(self) -> tuple[View, str | None]:
        resp = self._ask("toggle")
        if resp is None:
            view = self._offline()
            return view, _t("not_running", view.lang)
        return self.load(), None

    def new_session(self) -> tuple[View, str | None]:
        resp = self._ask("newsession")
        if resp is None:
            view = self._offline()
            return view, _t("not_running_note", view.lang, note=_t("offline_note", view.lang))
        if "error" in resp:
            return self.load(), resp["error"]
        view = self._online(resp)
        session = resp.get("session", "")
        if f"session.{session}" not in TEXT:
            return view, None
        return view, _t(f"session.{session}", view.lang, model=model_label(view.model, view.lang))


# ---- the window ----

def _row(label: str, value: str) -> Text:
    pad = " " * max(LABEL_CELLS - cell_len(label), 1)
    return Text(f"{label}{pad}{value}")


ROWS = ("speed", "voice", "model", "mic", "switch", "pause", "language")

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
                 on_preview: Callable[[int], None], lang: str | None = None) -> None:
        super().__init__()
        self._lang = lang
        self._groups = groups
        self._current = current
        self._last = current  # the voice highlighted without being previewed (at open)
        self._on_preview = on_preview

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(_t("voice.title", self._lang), id="title")
            yield Static(voice_label(self._groups, self._current, self._lang), id="status")
        options: list[Option] = []
        for g in self._groups:
            options.append(Option(Text(g.name, style="bold"), disabled=True))
            for v in g.voices:
                options.append(Option(Text(f"   {v.style}"), id=f"v{v.id}"))
        yield OptionList(*options, id="voices")
        yield Static("", id="msg")
        yield Static(_t("voice.keys", self._lang), id="keys")

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
            yield Static(_t("title", self.view.lang), id="title")
            yield Static("", id="status")
        yield OptionList(*(Option(Text(""), id=r) for r in ROWS), id="rows")
        yield Static("", id="hint")
        yield Static("", id="msg")
        yield Static(_t("keys", self.view.lang), id="keys")

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

    @property
    def lang(self) -> str:
        return self.view.lang or i18n.language()

    def _render_all(self) -> None:
        v, lang = self.view, self.lang
        if v.online:
            status = f"● {status_label({'state': v.state, 'text': v.state_text, 'reasons': list(v.reasons)}, lang)}"
        else:
            status = _t("status.stopped", lang, note=_t("offline_note", lang))
        main = self._main
        main.query_one("#title", Static).update(_t("title", lang))
        main.query_one("#keys", Static).update(_t("keys", lang))
        main.query_one("#status", Static).update(status)
        rows = main.query_one("#rows", OptionList)
        values = {
            "speed": _row(_t("row.speed", lang), f"◂ {v.speed:.1f} ▸"),
            "voice": _row(_t("row.voice", lang), f"{voice_label(self._groups, v.speaker_id, lang)}  ›"),
            "model": _row(_t("row.model", lang), f"◂ {model_label(v.model, lang)} ▸"),
            "mic": _row(_t("row.mic", lang), f"◂ {mic_text(self._mics, v.mic_device, self._default_mic, lang)} ▸"),
            "switch": _row(_t("row.switch", lang), _t("switch.value", lang)),
            "pause": _row(_t("row.pause", lang),
                          _t("pause.paused", lang) if v.paused else _t("pause.listening", lang)
                          if v.online else "—"),
            "language": _row(_t("row.language", lang), f"◂ {LANGUAGE_LABEL.get(lang, lang)} ▸"),
        }
        for rid, prompt in values.items():
            rows.replace_option_prompt(rid, prompt)
        self._render_hint()

    def _render_hint(self) -> None:
        row, lang = self._row(), self.lang
        if row == "model":
            hint = model_note(self.view, lang) or _t("hint.model", lang)
        elif row == "mic":
            hint = mic_note(self.view, self._default_mic, lang)
        elif row == "speed":
            hint = _t("hint.speed", lang, min=f"{SPEED_MIN:.1f}", max=f"{SPEED_MAX:.1f}")
        elif row in ("voice", "switch", "pause", "language"):
            hint = _t(f"hint.{row}", lang)
        else:
            hint = ""
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
            self._schedule_preview(self.view.speaker_id, _t("sample.speed", self.lang))
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
        elif row == "language":
            lang = other_language(self.lang, delta)
            self.view.lang = lang  # redraw in it now; the reply (config.toml) confirms it
            self._render_all()
            await self._set("language", lang)

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
            self._say(_t("no_voicevox", self.lang))
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
                        lambda sid: self._schedule_preview(sid, _t("sample.voice", self.lang)), self.lang),
            picked,
        )

    # ---- previews ----

    def _schedule_preview(self, speaker_id: int | None, text: str) -> None:
        if not self.view.online or speaker_id is None:
            if not self.view.online:
                self._say(_t("stopped", self.lang))
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
