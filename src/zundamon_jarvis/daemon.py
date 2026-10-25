"""`zundamon-jarvis daemon`: the always-listening assistant.

Mic (AudioIn) -> SileroVad -> Endpointer -> Orchestrator, plus the control socket
(``zundamon-jarvisctl``), the conversation log and a VOICEVOX reachability watch.

- Pause closes the mic stream (not a single frame is read while paused) and resets the
  endpointer and the VAD; resume resets them again, reopens the mic, then resumes.
- VOICEVOX unreachable -> state ``offline``; checked again every 30 s (every 2 s until the
  speaker is resolved; nothing is synthesized before that, so never in the wrong voice).
- Mic watchdog: no frame for 3 s while listening (e.g. a USB drop) -> close, reset the VAD and
  the endpointer, reopen; after 3 failures in a row the state is ``offline`` until frames
  arrive again. Exact digital silence (every sample 0) for 5 s while listening counts as the
  same kind of stall (a webcam mic can keep delivering frames of zeros until it is reopened);
  a settings preview's deliberate mute is exempt, and a quiet room (tiny non-zero noise) never trips it.
- Half duplex (``duplex`` in the config; ``auto`` = a Mac playing through its built-in speakers,
  re-checked each time ZundamonJarvis starts speaking): while it is saying something, mic frames
  are read but go nowhere (no VAD, no endpointer, so no barge-in); HALF_DUPLEX_TAIL_S after it
  stops (the echo tail; a gap between streamed sentences stays muted) the endpointer and the
  VAD are reset once. Dropped frames still count as arrived (watchdog) and never as digital
  silence. Full duplex (always on Linux unless ``half`` is set) is the plain pipeline.
  ``auto`` sees the output PortAudio found when the daemon started: after switching outputs
  (plugging in headphones), restart the daemon, or set ``duplex = "half"``.
- Startup: if another daemon holds the socket, exit 3 (EXIT_ALREADY_RUNNING) before any heavy
  import; the unit has RestartPreventExitStatus=3.
- Stuck audio (``player.Player`` or ``audio_in.AudioIn`` gave up on PortAudio, e.g. after
  PipeWire restarted):
  shut down cleanly and exit 4 (EXIT_AUDIO_STUCK) so the service manager restarts us with a
  fresh audio stack; if the clean shutdown hangs too, exit hard after HARD_EXIT_S
  (``AudioGuard``).
- Watch: heard transcripts, what is said, tool uses, confirmations and state changes go to
  the control socket's ``WatchHub`` (``zundamon-jarvisctl watch``) through ``activity.ActivityFeed``.
- Settings (``live_settings.LiveSettings`` on the control socket: ``get`` / ``set`` / ``preview``
  / ``newsession``): speed and voice change the live Voicevox, the model the next brain;
  a preview plays on its own player with the mic ignored meanwhile (``set_ears_muted``).
- SIGTERM / SIGINT -> ``orchestrator.aclose()`` first (closes the Claude session, so the
  SessionEnd hook captures the conversation), then the mic and the log; exit 0.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from zundamon_jarvis import control, platform
from zundamon_jarvis.diag import RateLimitedLog, Window
from zundamon_jarvis.endpoint import SpeechStarted, Utterance
from zundamon_jarvis.mics import output_is_builtin_speaker

log = logging.getLogger(__name__)

EXIT_ALREADY_RUNNING = 3
EXIT_AUDIO_STUCK = 4
HARD_EXIT_S = 20.0
MIC_RETRY_S = 5.0
MIC_STALL_S = 3.0  # no frame for this long while listening -> reopen the mic
MIC_MAX_FAILURES = 3  # in a row -> offline
DIGITAL_SILENCE_S = 5.0  # all-zero frames this long while listening -> reopen the mic
SAMPLE_RATE = 16000
TTS_RETRY_S = 30.0
TTS_RESOLVE_RETRY_S = 2.0
HALF_DUPLEX_TAIL_S = 0.3  # half duplex: the mic stays muted this long after speaking (echo tail)
STUCK_WARN_S = 30.0  # in speech / user_talking this long -> one WARNING per episode


class _MicStalled(Exception):
    pass


class _MicSilent(Exception):
    """Frames keep arriving but every sample is exactly 0."""


class TtsUnavailable(Exception):
    """VOICEVOX can't be used yet (the speaker is not resolved)."""


class _Audio(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def frames(self) -> Any: ...


class Daemon:
    def __init__(
        self,
        audio: _Audio,
        vad: Any,
        endpointer: Any,
        orch: Any,
        log_: Any = None,
        *,
        start_paused: bool = False,
        services: Iterable[Callable[[], Awaitable[None]]] = (),
        state_observers: Iterable[Callable[[str], None]] = (),
        mic_retry_s: float = MIC_RETRY_S,
        mic_stall_s: float = MIC_STALL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        half_duplex: Callable[[], bool] | None = None,
    ) -> None:
        self._audio = audio
        self._vad = vad
        self._endpointer = endpointer
        self._orch = orch
        self._log = log_
        self._start_paused = start_paused
        self._services = list(services)
        self._state_observers = list(state_observers)
        self._mic_retry_s = mic_retry_s
        self._mic_stall_s = mic_stall_s
        self._sleep = sleep
        self._mic_failures = 0
        self._zero_samples = 0
        self._offline: set[str] = set()  # "tts", "mic"
        self._deaf = False  # a settings preview is playing: mic frames are ignored
        # None = full duplex. Else asked when speaking starts: drop the mic while it lasts?
        self._half_duplex = half_duplex
        self._was_saying = False
        self._half_muted = False  # speaking in half duplex: mic frames are dropped
        self._half_until: float | None = None  # speaking ended: still dropped until then
        self._half_last: bool | None = None  # the last answer, to log only changes
        # Set by the assembly to LiveSettings.stop_preview: pause/resume end any preview.
        self.interrupt_preview: Callable[[], None] = lambda: None

        # diagnostics (``debug`` on the control socket, speech-event log lines)
        self._clock = clock
        self._frames_seen = Window(5.0, clock=clock)
        self._peaks = Window(5.0, clock=clock)  # per-frame abs-max, unrounded
        self._probs = Window(30.0, clock=clock)
        self._speech_since: float | None = None  # last SpeechStarted, while in speech
        self._talking_since: float | None = None  # orchestrator user_talking became True
        self._warned_speech = self._warned_talking = False
        self._rlog = RateLimitedLog(log, clock=clock)

        self.paused = False
        self.started = asyncio.Event()
        self._stopping = asyncio.Event()
        self._resumed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        orch.on_state_change = self._on_state

    @property
    def state(self) -> str:
        return self._orch.state

    @property
    def login_ok(self) -> bool:
        return self._orch.login_ok

    @property
    def offline_reasons(self) -> set[str]:
        return set(self._offline)

    def set_offline(self, reason: str, offline: bool) -> None:
        """Offline while any reason (VOICEVOX, the mic) holds."""
        if offline:
            self._offline.add(reason)
        else:
            self._offline.discard(reason)
        self._orch.set_offline(bool(self._offline))

    # ---- control (socket / hotkey) ----

    async def pause(self) -> None:
        async with self._lock:
            await self._pause()

    async def resume(self) -> None:
        async with self._lock:
            await self._resume()

    async def toggle(self) -> None:
        async with self._lock:
            if self.paused:
                await self._resume()
            else:
                await self._pause()

    async def _pause(self) -> None:
        if self.paused or self._stopping.is_set():
            return
        self._end_preview("pause")
        self.paused = True
        self._reset_silence_stats()
        self._was_saying = self._half_muted = False  # resume resets the ears anyway
        self._half_until = None
        self._resumed.clear()
        self._audio.close()  # the listen loop's frames() ends; nothing more is read
        self._endpointer.reset()
        self._vad.reset()
        await self._orch.pause()
        log.info("paused")

    async def _resume(self) -> None:
        if not self.paused or self._stopping.is_set():
            return
        self._end_preview("resume")
        self._endpointer.reset()
        self._vad.reset()
        try:
            self._audio.open()
        except Exception:
            log.exception("could not open the microphone; staying paused")
            return
        self.paused = False
        self._reset_silence_stats()
        await self._orch.resume()
        self._resumed.set()
        log.info("resumed")

    def stop(self) -> None:
        self._stopping.set()

    @property
    def ears_muted(self) -> bool:
        return self._deaf

    def set_ears_muted(self, muted: bool) -> None:
        """Ignore the mic while a settings preview plays (ZundamonJarvis must not hear its own
        sample as an utterance); afterwards the VAD and the endpointer start fresh."""
        if muted == self._deaf:
            return
        self._deaf = muted
        log.info("ears %s (settings preview)", "muted" if muted else "unmuted")
        if not muted:
            self._reset_ears("deaf")

    def _end_preview(self, why: str) -> None:
        """Pause/resume: stop a settings preview and never leave the ears muted."""
        try:
            self.interrupt_preview()
        except Exception:
            log.exception("stopping the preview failed")
        if self._deaf:
            self._deaf = False
            log.info("ears unmuted (%s)", why)

    def _reset_silence_stats(self) -> None:
        self._zero_samples = 0
        self._peaks.clear()

    def _reset_ears(self, why: str) -> None:
        """Fresh VAD and endpointer. If the user was mid-utterance, that utterance is gone:
        tell the orchestrator, or it would keep waiting for it (speech held, clocks stopped)."""
        was = self._endpointer.in_speech or self._orch.user_talking
        self._endpointer.reset()
        self._vad.reset()
        self._speech_since = None
        if was:
            log.info("utterance dropped (%s): the ears were reset mid-utterance", why)
            try:
                self._orch.on_speech_abandoned()
            except Exception:
                log.exception("orchestrator failed on abandoned speech")

    def debug(self) -> dict:
        """``jarvisctl debug``: where the ears are, to find out what gets stuck."""
        now = self._clock()
        ep = self._endpointer
        return {
            "paused": self.paused,
            "ears_muted": self._deaf,
            "half_duplex_muted": self._half_muted,
            "frames_last_5s": self._frames_seen.count(),
            "last_frame_age_s": self._frames_seen.last_age(),
            "mic_failures": self._mic_failures,
            "max_abs_last_5s": self._peaks.peak(),
            "zero_frames_streak_s": round(self._zero_samples / SAMPLE_RATE, 3),
            "mic_fallback": bool(getattr(self._audio, "fallback", False)),
            "vad": {"last_2s": self._probs.stats(last_s=2.0), "last_30s": self._probs.stats()},
            "endpointer": {
                "in_speech": bool(ep.in_speech),
                "since_speech_started_s": (round(now - self._speech_since, 3)
                                           if self._speech_since is not None else None),
                "utterance_s": round(float(ep.utterance_s), 3),
            },
            "orchestrator": self._orch.debug_state(),
            "offline_reasons": sorted(self._offline),
            # macOS grants the mic per executable: this is the one System Settings lists
            "python": os.path.realpath(sys.executable),
        }

    # ---- running ----

    async def run(self, install_signals: bool = True) -> None:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        if install_signals:
            for sig in signals:
                loop.add_signal_handler(sig, self.stop)
        tasks: list[asyncio.Task] = []
        try:
            if self._start_paused:
                async with self._lock:
                    self.paused = True
                    await self._orch.pause()
            tasks.append(asyncio.create_task(self._listen()))
            tasks.extend(asyncio.create_task(service()) for service in self._services)
            self.started.set()
            log.info("running (%s)", self.state)
            await self._stopping.wait()
        finally:
            self._stopping.set()
            try:
                await self._shutdown(tasks)
            finally:
                if install_signals:
                    for sig in signals:
                        loop.remove_signal_handler(sig)

    async def _shutdown(self, tasks: list[asyncio.Task]) -> None:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self._orch.aclose()  # the Claude session ends -> SessionEnd capture hook
        except Exception:
            log.exception("orchestrator close failed")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            self._audio.close()
        except Exception:
            log.exception("mic close failed")
        if self._log is not None:
            self._log.close()

    async def _listen(self) -> None:
        while not self._stopping.is_set():
            if self.paused:
                await self._resumed.wait()
                continue
            try:
                await self._read_mic()
                continue  # the stream was closed: paused, or stopping
            except asyncio.CancelledError:
                raise
            except _MicStalled:
                reason = f"no audio for {self._mic_stall_s:g} s"
            except _MicSilent:
                log.warning("microphone delivers digital silence; reopening")
                reason = f"digital silence for {DIGITAL_SILENCE_S:g} s"
            except Exception as e:  # opening or reading the stream failed
                log.exception("microphone error")
                reason = repr(e)
            await self._mic_failed(reason)

    async def _read_mic(self) -> None:
        frames = self._audio.frames()
        try:
            while True:
                try:
                    async with asyncio.timeout(self._mic_stall_s):
                        frame = await anext(frames)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if self.paused or self._stopping.is_set():
                        return
                    raise _MicStalled from None
                if self.paused:
                    return
                self._update_half_duplex()
                self._mic_alive(frame)
                self._frames_seen.add(1.0)
                self._check_stuck()
                await self._process(frame)
                if self.paused:
                    return
        finally:
            await frames.aclose()

    def _update_half_duplex(self) -> None:
        """Half duplex: start dropping when speaking starts (if the output says so). When it
        ends, keep dropping for HALF_DUPLEX_TAIL_S (the echo tail and frames still in flight;
        also the gap between streamed sentences), then reset the ears once."""
        if self._half_duplex is None:
            return
        saying = bool(self._orch.saying)
        if saying:
            if self._half_until is not None:  # speaking again within the hold: stay muted
                self._half_until = None
            elif not self._was_saying:
                self._half_muted = self._ask_half_duplex()
            self._was_saying = True
            return
        if self._was_saying:
            self._was_saying = False
            if self._half_muted:
                self._half_until = self._clock() + HALF_DUPLEX_TAIL_S
            return
        if self._half_until is not None and self._clock() >= self._half_until:
            self._half_until = None
            self._half_muted = False
            self._reset_ears("half duplex")

    def _ask_half_duplex(self) -> bool:
        assert self._half_duplex is not None
        try:
            half = bool(self._half_duplex())
        except Exception:
            log.exception("half duplex check failed; listening while speaking")
            half = False
        if half != self._half_last:
            self._half_last = half
            log.info("%s duplex while speaking", "half" if half else "full")
        return half

    async def _process(self, frame: Any) -> None:
        if self._deaf or self._half_muted:
            return
        prob = self._vad.prob(frame)
        self._probs.add(prob)
        for ev in self._endpointer.feed(frame, prob):
            if isinstance(ev, SpeechStarted):
                self._speech_since = self._clock()
                self._warned_speech = False
                self._rlog(logging.INFO, "speech", "speech started (vad %.2f)", prob)
                try:
                    await self._orch.on_speech_started()
                except Exception:
                    log.exception("orchestrator failed on speech start")
            elif isinstance(ev, Utterance):
                self._speech_since = None
                seconds = len(ev.audio) / SAMPLE_RATE
                if getattr(ev, "cut", False):
                    log.warning("utterance cut at %.1f s: the VAD never fell silent; "
                                "resetting the VAD", seconds)
                    self._vad.reset()
                else:
                    self._rlog(logging.INFO, "utterance", "utterance %.1f s", seconds)
                self._spawn(self._orch.on_utterance(ev.audio))
                await asyncio.sleep(0)  # its synchronous start runs before the next frame

    def _check_stuck(self) -> None:
        now = self._clock()
        if self._endpointer.in_speech and self._speech_since is not None:
            if not self._warned_speech and now - self._speech_since > STUCK_WARN_S:
                self._warned_speech = True
                log.warning("endpointer in speech for %.0f s (utterance %.1f s, vad 2 s %s)",
                            now - self._speech_since, self._endpointer.utterance_s,
                            self._probs.stats(last_s=2.0))
        if self._orch.user_talking:
            if self._talking_since is None:
                self._talking_since, self._warned_talking = now, False
            elif not self._warned_talking and now - self._talking_since > STUCK_WARN_S:
                self._warned_talking = True
                log.warning("orchestrator user_talking for %.0f s (endpointer in_speech=%s)",
                            now - self._talking_since, self._endpointer.in_speech)
        else:
            self._talking_since = None

    def _mic_alive(self, frame: Any) -> None:
        peak = float(np.max(np.abs(frame))) if len(frame) else 0.0
        self._peaks.add(peak)
        if peak > 0.0:
            if self._mic_failures:
                self._mic_failures = 0
                if "mic" in self._offline:
                    log.warning("microphone is back")
                    self.set_offline("mic", False)
            self._zero_samples = 0
            return
        if self._deaf or self._half_muted:  # deliberately ignored: not the mic's fault
            self._zero_samples = 0
            return
        self._zero_samples += len(frame)
        if self._zero_samples >= DIGITAL_SILENCE_S * SAMPLE_RATE:
            raise _MicSilent

    async def _mic_failed(self, reason: str) -> None:
        self._mic_failures += 1
        self._zero_samples = 0
        try:
            self._audio.close()
        except Exception:
            log.exception("mic close failed")
        self._reset_ears("mic failure")  # a fresh start on the reopened stream
        delay = 0.0 if self._mic_failures == 1 else self._mic_retry_s
        log.warning("microphone failed (%s); reopening%s (attempt %d)", reason,
                    f" in {delay:g} s" if delay else "", self._mic_failures)
        if self._mic_failures == MIC_MAX_FAILURES:
            log.error("microphone unavailable after %d attempts; state offline until audio "
                      "arrives again (still retrying every %g s)", MIC_MAX_FAILURES,
                      self._mic_retry_s)
            self.set_offline("mic", True)
        if delay:
            await self._sleep(delay)
        else:
            await asyncio.sleep(0)

    def _on_state(self, state: str) -> None:
        log.info("state: %s", state)
        for observe in ([self._log.on_state] if self._log is not None else []) + self._state_observers:
            try:
                observe(state)
            except Exception:
                log.exception("state observer failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def duplex_check(
    duplex: str, system: str, builtin_speaker: Callable[[str], bool] = output_is_builtin_speaker,
) -> Callable[[], bool] | None:
    """The daemon's ``half_duplex``: None for full duplex, else asked as speaking starts."""
    if duplex == "full":
        return None
    if duplex == "half":
        return lambda: True
    if system != "mac":  # auto on Linux: full duplex, as it always was
        return None
    return lambda: builtin_speaker(system)


class TtsWatch:
    """Keeps ``offline`` in step with VOICEVOX reachability; resolves the speaker first."""

    def __init__(
        self,
        voicevox: Any,
        set_offline: Callable[[bool], None],
        speaker_name: str | None = None,
        interval_s: float = TTS_RETRY_S,
        resolve_interval_s: float = TTS_RESOLVE_RETRY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._vv = voicevox
        self._set_offline = set_offline
        self._speaker_name = speaker_name
        self._interval = interval_s
        self._resolve_interval = resolve_interval_s
        self._sleep = sleep
        self._online: bool | None = None

    @property
    def resolved(self) -> bool:
        return self._speaker_name is None

    def choose(self, speaker_id: int) -> None:
        """Use this style id from now on; a pending name lookup (青山龍星) is dropped for good."""
        self._vv.speaker_id = speaker_id
        self._speaker_name = None

    def _mark(self, online: bool, why: object = None) -> None:
        if online == self._online:
            return
        if online:
            if self._online is False:
                log.info("VOICEVOX is back")
        else:
            log.warning("VOICEVOX unavailable (%s); retrying every %g s", why,
                        self._interval if self.resolved else self._resolve_interval)
        self._online = online
        self._set_offline(not online)

    async def check(self) -> bool:
        try:
            if self._speaker_name is not None:
                name = self._speaker_name
                sid = await self._vv.find_speaker(name)
                if self._speaker_name is not None:  # choose() meanwhile wins
                    self._vv.speaker_id = sid
                    log.info("speaker %s = %d", name, sid)
                    self._speaker_name = None
            else:
                await self._vv.version()
        except (httpx.HTTPError, OSError, LookupError) as e:
            self._mark(False, e)
            return False
        self._mark(True)
        return True

    async def run(self) -> None:
        while True:
            await self.check()
            await self._sleep(self._interval if self.resolved else self._resolve_interval)

    def guard_synth(self, synth: Callable[[str], Awaitable[Any]]) -> Callable[[str], Awaitable[Any]]:
        """No synthesis (prefetches included) before the speaker is known: never the wrong voice."""

        async def wrapped(text: str) -> Any:
            if not self.resolved:
                raise TtsUnavailable("speaker not resolved yet")
            return await synth(text)

        return wrapped

    def guard_speak(self, speak: Callable[[str], Awaitable[bool]]) -> Callable[[str], Awaitable[bool]]:
        async def wrapped(text: str) -> bool:
            try:
                if not self.resolved:
                    raise TtsUnavailable("speaker not resolved yet")
                ok = await speak(text)
            except (httpx.TransportError, TtsUnavailable) as e:
                self._mark(False, e)
                return False
            self._mark(True)
            return ok

        return wrapped


class AudioGuard:
    """``Player`` / ``AudioIn`` ``on_stuck``: the first report stops the daemon and arms a hard
    exit (the clean shutdown can hang on the same stream)."""

    def __init__(
        self,
        stop: Callable[[], None],
        hard_exit: Callable[[int], Any] = os._exit,
        timer: Callable[..., Any] = threading.Timer,
    ) -> None:
        self._stop = stop
        self._hard_exit = hard_exit
        self._timer = timer
        self.exit_code = 0

    def __call__(self, why: str) -> None:
        if self.exit_code:
            return
        self.exit_code = EXIT_AUDIO_STUCK
        log.error("audio output is stuck (%s): restarting", why)
        t = self._timer(HARD_EXIT_S, self._hard_exit, args=(EXIT_AUDIO_STUCK,))
        t.daemon = True
        t.start()
        self._stop()


# ---- assembly ----

async def _amain(start_paused: bool, sock: Path) -> int:
    # Heavy imports here: after the already-running check, and tests of the pieces above stay light.
    from zundamon_jarvis.activity import ActivityFeed
    from zundamon_jarvis.audio_in import AudioIn
    from zundamon_jarvis.brain import ClaudeBrain
    from zundamon_jarvis.config import load_config
    from zundamon_jarvis.convlog import (
        ConversationLog,
        legacy_log_dir,
        log_dir,
        logged_speak,
        logged_transcribe,
        migrate_legacy_logs,
        prune,
    )
    from zundamon_jarvis.endpoint import Endpointer
    from zundamon_jarvis.live_settings import LiveSettings
    from zundamon_jarvis.orchestrator import Orchestrator, SynthSpeaker
    from zundamon_jarvis.player import Player
    from zundamon_jarvis.stt import make_transcriber
    from zundamon_jarvis.text import is_hallucination
    from zundamon_jarvis.tts import DEFAULT_SPEAKER, RATE, Voicevox
    from zundamon_jarvis.vad import SileroVad

    cfg = load_config()
    directory = log_dir()
    if moved := migrate_legacy_logs(directory, legacy_log_dir()):
        log.info("moved %d log(s) from %s to %s", len(moved), legacy_log_dir(), directory)
    for p in prune(directory, date.today()):
        log.info("removed old log %s", p.name)

    log.info("loading speech recognition")
    transcriber = await asyncio.to_thread(make_transcriber)
    loop = asyncio.get_running_loop()

    vv = Voicevox(speaker_id=cfg.speaker_id if cfg.speaker_id is not None else 0, speed=cfg.speed)
    watch = TtsWatch(vv, lambda offline: daemon.set_offline("tts", offline),
                     speaker_name=DEFAULT_SPEAKER if cfg.speaker_id is None else None)
    guard = AudioGuard(lambda: daemon.stop())
    player = Player(on_stuck=guard)
    speaker = SynthSpeaker(watch.guard_synth(vv.synth), lambda pcm: player.play(pcm, RATE),
                           player.stop)
    clog = ConversationLog(directory)
    hub = control.WatchHub()
    feed = ActivityFeed(hub.publish, offline_reasons=lambda: daemon.offline_reasons)

    def on_heard(text: str) -> None:
        text = (text or "").strip()
        if not text or is_hallucination(text) or daemon.paused:
            return
        clog.heard(text, orch.state)
        feed.heard(text)

    def new_brain() -> ClaudeBrain:  # cfg.model is changed live by the settings window
        live.brain_started(cfg.model)
        return ClaudeBrain(cfg.workdir, [cfg.knowledge_dir] if cfg.knowledge_dir else [],
                           model=cfg.model)

    def on_say(text: str, kind: str) -> None:
        live.stop_preview()  # ZundamonJarvis itself speaking cuts a settings sample
        feed.said(text, kind)

    orch = Orchestrator(
        new_brain,
        transcribe=logged_transcribe(transcriber.transcribe, loop, on_heard),
        speak=watch.guard_speak(logged_speak(speaker.speak, clog.replied)),
        stop_speaking=speaker.stop,
        prefetch=speaker.prefetch,
    )
    orch.on_brain_event = feed.brain_event
    orch.on_say = on_say
    orch.on_confirm = feed.confirm
    system = platform.system()
    daemon = Daemon(AudioIn(device=cfg.mic_device, system=system, on_stuck=guard),
                    SileroVad(), Endpointer(),
                    orch, clog, start_paused=start_paused, services=[watch.run],
                    state_observers=[feed.state],
                    half_duplex=duplex_check(cfg.duplex, system))
    preview_player = Player(on_stuck=guard)  # its own stream: never clobbers the reply player
    live = LiveSettings(cfg, vv, watch, orch,
                        play=lambda pcm: preview_player.play(pcm, RATE),
                        stop_playback=preview_player.stop, mute_ears=daemon.set_ears_muted)
    daemon.interrupt_preview = live.stop_preview
    feed.state(orch.state)
    await watch.check()

    try:
        server = await control.serve(daemon, sock, hub=hub, settings=live)
    except RuntimeError as e:  # another daemon won the race
        log.error("%s", e)
        return EXIT_ALREADY_RUNNING
    try:
        await daemon.run()
    finally:
        hub.close()  # open watch connections would keep wait_closed() waiting
        server.close()
        await server.wait_closed()
        sock.unlink(missing_ok=True)
        await live.aclose()
        await vv.aclose()
    log.info("bye")
    return guard.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zundamon-jarvis daemon")
    parser.add_argument("--start-paused", action="store_true",
                        help="start with the mic closed (resume with `zundamon-jarvisctl toggle`)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # no line per VOICEVOX ping
    sock = control.socket_path()
    if control.is_live(sock):  # before the heavy imports: a restart loop stays cheap
        log.error("zundamon-jarvis is already running (%s)", sock)
        return EXIT_ALREADY_RUNNING
    code = asyncio.run(_amain(args.start_paused, sock))
    if code == EXIT_AUDIO_STUCK:  # PortAudio's own exit handler would wait on the stuck stream
        logging.shutdown()
        os._exit(code)
    return code
