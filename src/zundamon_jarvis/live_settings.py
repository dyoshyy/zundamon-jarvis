"""Settings the running daemon can change live (the settings window, ``zundamon-jarvisctl set``).

- ``speed``: VOICEVOX ``speedScale`` for everything said from now on.
- ``speaker``: a VOICEVOX style id (checked against the engine's ``/speakers``). It replaces
  the startup 青山龍星 lookup for good (``TtsWatch.choose``), so a late lookup never undoes it.
- ``model``: sonnet / opus / haiku for the *next* Claude session (the brain factory reads
  ``cfg.model``); ``new_session`` closes the current session at once, but only when it is idle
  (the same check as the 30-min idle close), so the next utterance starts with the new model.
- ``mic``: ``default`` (follow the OS default input), a PipeWire source name (Linux), a
  CoreAudio input device name (Mac) or a PortAudio device index. Saved only: the open stream
  keeps its device until ZundamonJarvis restarts (``snapshot()["mic_active"]`` is the one in
  use, ``["mic_device"]`` the saved one).
- ``language``: ``ja`` / ``en``, the display language of the windows, menus and setup
  (``zundamon_jarvis.i18n``). Saved only: nothing in the daemon shows text, and the windows
  read config.toml the next time they draw. What ZundamonJarvis says follows the speaker.

Every change goes to the running objects first, then to config.toml (``update_config``: only
that key). A config file that can't be written (broken TOML) leaves the change applied live and
reports ``saved: false``.

``preview(speaker, speed, text)`` synthesizes a sample with the given voice and speed (the live
ones are not touched) and plays it once on its own player, even while paused. Never over the
user (mid-utterance) or over ZundamonJarvis saying anything: then it answers ``busy`` and plays
nothing. While the sample plays the mic input is ignored (``mute_ears``), so ZundamonJarvis can't hear
its own sample as an utterance. A newer preview replaces the one playing; ZundamonJarvis starting to
say something cuts it (``stop_preview``), and so do pause/resume (the daemon's
``interrupt_preview``). The mute starts in the same step as the last busy check and always
ends: playback is bounded to the sample's length + 2 s, and failures unmute too.
"""

import asyncio
import logging
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from zundamon_jarvis.config import CONFIG_PATH, LANGUAGES, MODELS, SPEED_MAX, SPEED_MIN, Config, update_config, valid_speed
from zundamon_jarvis.mics import parse_mic
from zundamon_jarvis.tts import RATE, talk_styles

log = logging.getLogger(__name__)

PREVIEW_MAX_CHARS = 200
SYNTH_TIMEOUT_S = 8.0
SPEAKERS_TIMEOUT_S = 3.0
SPEAKER_ID_MAX = 100_000
PLAY_MARGIN_S = 2.0  # a sample may play this much longer than its length before it is cut
SETTABLE = ("speed", "speaker", "model", "mic", "language")


def parse_speed(raw: str) -> float:
    raw = raw.strip()
    if not re.fullmatch(r"\d+(\.\d+)?", raw):
        raise ValueError(f"speed must be a number {SPEED_MIN:g}..{SPEED_MAX:g}: {raw!r}")
    value = float(raw)
    speed = valid_speed(value) if math.isfinite(value) else None
    if speed is None:
        raise ValueError(f"speed must be a number {SPEED_MIN:g}..{SPEED_MAX:g}: {raw!r}")
    return speed


def parse_speaker(raw: str) -> int:
    raw = raw.strip()
    if not re.fullmatch(r"\d{1,6}", raw) or int(raw) >= SPEAKER_ID_MAX:
        raise ValueError(f"speaker must be a VOICEVOX style id: {raw!r}")
    return int(raw)


def parse_model(raw: str) -> str:
    raw = raw.strip()
    if raw not in MODELS:
        raise ValueError(f"model must be one of {'/'.join(MODELS)}: {raw!r}")
    return raw


def parse_language(raw: str) -> str:
    raw = raw.strip()
    if raw not in LANGUAGES:
        raise ValueError(f"language must be one of {'/'.join(LANGUAGES)}: {raw!r}")
    return raw


def parse_text(raw: str) -> str:
    text = raw.strip()
    if not text or len(text) > PREVIEW_MAX_CHARS:
        raise ValueError(f"preview text must be 1..{PREVIEW_MAX_CHARS} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise ValueError("preview text must not contain control characters")
    return text


class LiveSettings:
    def __init__(
        self,
        cfg: Config,
        voicevox: Any,
        watch: Any,
        orch: Any,
        *,
        play: Callable[[Any], Awaitable[bool]],
        stop_playback: Callable[[], None],
        mute_ears: Callable[[bool], None],
        path: Path = CONFIG_PATH,
        rate: int = RATE,
        play_margin_s: float = PLAY_MARGIN_S,
    ) -> None:
        self._cfg = cfg
        self._vv = voicevox
        self._watch = watch
        self._orch = orch
        self._play = play
        self._stop_playback = stop_playback
        self._mute_ears = mute_ears
        self._path = path
        self._rate = rate
        self._play_margin = play_margin_s
        self._session_model: str | None = None
        self._mic_active = cfg.mic_device  # what the open stream uses; config changes need a restart
        self._task: asyncio.Task | None = None
        self._generation = 0

    # ---- state ----

    def brain_started(self, model: str | None) -> None:
        """The brain factory calls this: which model the open session runs on."""
        self._session_model = model

    def snapshot(self) -> dict:
        return {
            "speed": self._vv.speed,
            "speaker_id": self._vv.speaker_id if self._watch.resolved else None,
            "model": self._cfg.model,
            "session_model": self._session_model if self._orch.has_session else None,
            "models": list(MODELS),
            "mic_device": self._cfg.mic_device,
            "mic_active": self._mic_active,
            "speed_min": SPEED_MIN,
            "speed_max": SPEED_MAX,
            "language": self._cfg.language,  # None: the OS's
        }

    # ---- set ----

    async def set(self, key: str, raw: str) -> dict:
        try:
            if key == "speed":
                speed = parse_speed(raw)
                self._vv.speed = self._cfg.speed = speed
                saved = self._save({"speed": speed})
            elif key == "speaker":
                sid = parse_speaker(raw)
                await self._check_speaker(sid)
                self._watch.choose(sid)
                self._cfg.speaker_id = sid
                saved = self._save({"speaker_id": sid})
            elif key == "model":
                model = parse_model(raw)
                self._cfg.model = model
                saved = self._save({"model": model})
            elif key == "mic":
                mic = parse_mic(raw)
                self._cfg.mic_device = mic
                saved = self._save({"mic_device": mic})  # None removes the line
            elif key == "language":
                lang = parse_language(raw)
                self._cfg.language = lang
                saved = self._save({"language": lang})
            else:
                return {"error": f"unknown setting: {key!r} (use {'/'.join(SETTABLE)})"}
        except ValueError as e:
            return {"error": str(e)}
        log.info("setting %s = %s%s", key, raw.strip(), "" if saved else " (not saved)")
        return {"ok": True, "saved": saved, "settings": self.snapshot()}

    async def _check_speaker(self, sid: int) -> None:
        try:
            speakers = await asyncio.wait_for(self._vv.speakers(), SPEAKERS_TIMEOUT_S)
        except (httpx.HTTPError, OSError, TimeoutError) as e:
            raise ValueError(f"VOICEVOX unreachable, can't check speaker {sid}: {e!r}") from None
        if sid not in {i for _, _, i in talk_styles(speakers)}:
            raise ValueError(f"no VOICEVOX speaking style with id {sid}")

    def _save(self, updates: dict) -> bool:
        try:
            update_config(updates, self._path)
        except (OSError, ValueError, TypeError):
            log.exception("could not save %s to %s", sorted(updates), self._path)
            return False
        return True

    # ---- session ----

    def new_session(self) -> dict:
        """``closed`` (the next utterance starts afresh), ``none`` (no session) or ``busy``."""
        return {"session": self._orch.restart_session(), "settings": self.snapshot()}

    # ---- preview ----

    @property
    def previewing(self) -> bool:
        return self._task is not None and not self._task.done()

    def _busy(self) -> str | None:
        if self._orch.user_talking:
            return "user_speaking"
        if self._orch.saying:
            return "speaking"
        return None

    async def preview(self, speaker_raw: str, speed_raw: str, text_raw: str) -> dict:
        try:
            sid = parse_speaker(speaker_raw)
            speed = parse_speed(speed_raw)
            text = parse_text(text_raw)
        except ValueError as e:
            return {"error": str(e)}
        if busy := self._busy():
            return {"ok": False, "busy": busy}
        await self._replace()
        generation = self._generation
        try:
            pcm = await asyncio.wait_for(self._vv.synth(text, sid, speed=speed), SYNTH_TIMEOUT_S)
        except (httpx.HTTPError, OSError, TimeoutError) as e:
            return {"error": f"VOICEVOX unavailable: {e!r}"}
        except Exception as e:  # a malformed engine reply etc.: still an answer, never silence
            log.exception("preview synthesis failed")
            return {"error": f"preview failed: {e!r}"}
        if generation != self._generation:  # a newer preview (or ZundamonJarvis) came in meanwhile
            return {"ok": False, "busy": "replaced"}
        if busy := self._busy():
            return {"ok": False, "busy": busy}
        # Mute now, in the same step as the busy check: no frame can start an utterance between
        # them (one would be cut off by the mute and leave the orchestrator waiting for it).
        self._mute_ears(True)
        try:
            self._task = asyncio.create_task(self._play_once(pcm))
        except BaseException:
            self._mute_ears(False)
            raise
        # Cancelled before it ever ran (stop_preview right away), its finally never runs.
        self._task.add_done_callback(lambda t: t.cancelled() and self._mute_ears(False))
        return {"ok": True}

    async def _play_once(self, pcm: Any) -> None:
        """Play the (already muted) sample once; the ears come back however it ends. The play is
        bounded: a sink that never reports the end (suspended, switched, a BT drop) is cut."""
        limit = len(pcm) / self._rate + self._play_margin
        try:
            await asyncio.wait_for(self._play(pcm), limit)
        except TimeoutError:
            log.warning("preview playback did not finish within %.1f s; stopping it", limit)
            try:
                self._stop_playback()
            except Exception:
                log.exception("preview stop failed")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("preview playback failed")
        finally:
            self._mute_ears(False)

    def stop_preview(self) -> None:
        """Cut the sample now (ZundamonJarvis is about to say something, or a newer preview)."""
        self._generation += 1
        task = self._task
        if task is not None and not task.done():
            try:
                self._stop_playback()
            except Exception:
                log.exception("preview stop failed")
            task.cancel()

    async def _replace(self) -> None:
        self.stop_preview()
        task, self._task = self._task, None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        await self._replace()
