import asyncio
import os
import signal
import sys

import httpx
import numpy as np
import pytest
from fakes import settle

from zundamon_jarvis.daemon import (
    DIGITAL_SILENCE_S,
    EXIT_AUDIO_STUCK,
    HARD_EXIT_S,
    AudioGuard,
    HALF_DUPLEX_TAIL_S,
    SAMPLE_RATE,
    Daemon,
    TtsWatch,
    duplex_check,
)
from zundamon_jarvis.endpoint import SpeechStarted, Utterance


class Calls(list):
    pass


class FakeAudio:
    def __init__(self, calls):
        self.calls = calls
        self.is_open = False
        self.frames_started = 0
        self._q: asyncio.Queue = asyncio.Queue()
        self._gen = 0
        self.fail_opens = 0

    def open(self):
        self.calls.append("audio.open")
        if self.fail_opens:
            self.fail_opens -= 1
            raise OSError("no mic")
        self.is_open = True

    def close(self):
        self.calls.append("audio.close")
        self.is_open = False
        self._gen += 1
        self._q.put_nowait(None)  # wakes a waiting frames()

    def push(self, frame):
        if self.is_open:  # a closed stream reads nothing
            self._q.put_nowait(frame)

    async def frames(self):
        if not self.is_open:
            self.open()
        self.frames_started += 1
        gen = self._gen
        while True:
            frame = await self._q.get()
            if gen != self._gen:
                return
            if frame is not None:
                yield frame


class FakeVad:
    def __init__(self, calls):
        self.calls = calls
        self.probs = 0

    def prob(self, frame):
        self.probs += 1
        return frame.p

    def reset(self):
        self.calls.append("vad.reset")


class FakeEndpointer:
    def __init__(self, calls):
        self.calls = calls
        self.in_speech = False
        self.utterance_s = 0.0
        self.fed = 0

    def feed(self, frame, prob):
        self.fed += 1
        return frame.events

    def reset(self):
        self.calls.append("endpointer.reset")
        self.in_speech = False


class FakeOrch:
    def __init__(self, calls):
        self.calls = calls
        self.state = "listening"
        self.on_state_change = lambda s: None
        self.started = 0
        self.utterances = []
        self.user_talking = False
        self.saying = False  # Orchestrator.saying: something is being said right now
        self.abandoned = 0

    def on_speech_abandoned(self):
        self.abandoned += 1
        self.user_talking = False

    def debug_state(self):
        return {"user_talking": self.user_talking}

    def _set(self, state):
        self.state = state
        self.on_state_change(state)

    async def on_speech_started(self):
        self.started += 1

    async def on_utterance(self, audio):
        self.utterances.append(audio)

    async def pause(self):
        self.calls.append("orch.pause")
        self._set("paused")

    async def resume(self):
        self.calls.append("orch.resume")
        self._set("listening")

    def set_offline(self, offline):
        self.calls.append(f"orch.offline={offline}")

    async def aclose(self):
        self.calls.append("orch.aclose")
        await asyncio.sleep(0)  # a real close awaits the CLI


class FakeLog:
    def __init__(self, calls):
        self.calls = calls
        self.states = []

    def on_state(self, state):
        self.states.append(state)

    def close(self):
        self.calls.append("log.close")


class Frame(np.ndarray):
    """512 samples carrying the fake VAD probability and endpointer events."""

    p: float
    events: list


def frame(p=0.1, events=(), level=0.01):
    f = np.full(512, level, np.float32).view(Frame)
    f.p, f.events = p, list(events)
    return f


@pytest.fixture
def parts():
    calls = Calls()
    return calls, FakeAudio(calls), FakeVad(calls), FakeEndpointer(calls), FakeOrch(calls), FakeLog(calls)


def make(parts, **kw):
    calls, audio, vad, ep, orch, log = parts
    return Daemon(audio, vad, ep, orch, log, **kw)


async def started(d):
    task = asyncio.create_task(d.run(install_signals=False))
    await asyncio.wait_for(d.started.wait(), 1)
    await settle()
    return task


async def test_pause_closes_the_mic_then_resets_then_pauses(parts):
    calls, audio, *_ = parts
    d = make(parts)
    task = await started(d)
    calls.clear()
    await d.pause()
    assert calls == ["audio.close", "endpointer.reset", "vad.reset", "orch.pause"]
    assert d.state == "paused" and not audio.is_open
    d.stop()
    await task


async def test_resume_resets_then_opens_then_resumes(parts):
    calls, audio, *_ = parts
    d = make(parts)
    task = await started(d)
    await d.pause()
    calls.clear()
    await d.resume()
    assert calls == ["endpointer.reset", "vad.reset", "audio.open", "orch.resume"]
    assert d.state == "listening" and audio.is_open
    d.stop()
    await task


async def test_toggle_flips_and_repeats_are_noops(parts):
    calls, *_ = parts
    d = make(parts)
    task = await started(d)
    await d.toggle()
    assert d.state == "paused"
    calls.clear()
    await d.pause()
    assert calls == []
    await d.toggle()
    assert d.state == "listening"
    calls.clear()
    await d.resume()
    assert calls == []
    d.stop()
    await task


async def test_no_frame_is_read_while_paused(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    audio.push(frame())
    await settle()
    assert vad.probs == 1
    await d.pause()
    for _ in range(5):
        audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert vad.probs == 1 and orch.started == 0
    assert audio.frames_started == 1  # the loop did not ask for frames again
    await d.resume()
    audio.push(frame())
    await settle()
    assert vad.probs == 2 and audio.frames_started == 2
    d.stop()
    await task


async def test_start_paused_never_opens_the_mic(parts):
    calls, audio, *_ = parts
    d = make(parts, start_paused=True)
    task = await started(d)
    assert d.state == "paused"
    assert "audio.open" not in calls and audio.frames_started == 0
    assert "orch.pause" in calls
    await d.toggle()
    assert audio.is_open and d.state == "listening"
    d.stop()
    await task


async def test_endpointer_events_reach_the_orchestrator(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    pcm = np.zeros(10, np.float32)
    audio.push(frame(0.9, [SpeechStarted()]))
    audio.push(frame(0.1, [Utterance(pcm)]))
    await settle()
    assert orch.started == 1
    assert len(orch.utterances) == 1 and orch.utterances[0] is pcm
    d.stop()
    await task


async def test_muted_ears_ignore_the_mic_then_start_fresh(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    d.set_ears_muted(True)  # a settings preview is playing
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert vad.probs == 0 and orch.started == 0
    calls.clear()
    d.set_ears_muted(False)
    assert calls == ["endpointer.reset", "vad.reset"]  # nothing half-heard carries over
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert vad.probs == 1 and orch.started == 1
    calls.clear()
    d.set_ears_muted(False)  # repeated: nothing to do
    assert calls == []
    d.stop()
    await task


async def test_hearing_goes_on_at_speech_start_and_off_at_the_utterance(parts):
    calls, audio, vad, ep, orch, log = parts
    seen = []

    def broken(on):
        raise RuntimeError("watch broke")

    d = make(parts, hearing_observers=[broken, seen.append])
    task = await started(d)
    audio.push(frame(0.9, [SpeechStarted()]))
    audio.push(frame(0.9))  # still talking: no repeat
    audio.push(frame(0.1, [Utterance(np.zeros(10, np.float32))]))
    await settle()
    assert seen == [True, False]
    assert orch.started == 1  # a failing observer stops nothing
    d.stop()
    await task


async def test_hearing_goes_off_when_the_ears_stop_mid_utterance(parts):
    calls, audio, vad, ep, orch, log = parts
    seen = []
    d = make(parts, hearing_observers=[seen.append])
    task = await started(d)
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    d.set_ears_muted(True)  # a settings preview starts while the user talks
    audio.push(frame(0.9))
    await settle()
    assert seen == [True, False]
    audio.push(frame(0.9, [SpeechStarted()]))  # ignored while muted
    await settle()
    d.set_ears_muted(False)
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    await d.pause()
    assert seen == [True, False, True, False]
    d.stop()
    await task


async def test_state_changes_go_to_the_log(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    await d.pause()
    await d.resume()
    assert log.states == ["paused", "listening"]
    d.stop()
    await task


async def test_state_changes_go_to_the_observers_too(parts):
    seen = []

    def broken(state):
        raise RuntimeError("watch broke")

    d = make(parts, state_observers=[broken, seen.append])
    task = await started(d)
    await d.pause()
    await d.resume()
    assert seen == ["paused", "listening"]
    assert parts[5].states == ["paused", "listening"]  # a failing observer stops nothing
    d.stop()
    await task


async def test_resume_with_a_broken_mic_stays_paused(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    await d.pause()
    audio.fail_opens = 1
    await d.resume()
    assert d.state == "paused" and "orch.resume" not in calls
    await d.resume()  # mic back
    assert d.state == "listening"
    d.stop()
    await task


async def test_sigterm_closes_the_session_then_the_audio(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = asyncio.create_task(d.run())
    await asyncio.wait_for(d.started.wait(), 1)
    calls.clear()
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 2)
    assert calls.index("orch.aclose") < calls.index("audio.close")
    assert "log.close" in calls
    # the handlers are removed again
    assert not asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


async def test_sigint_also_shuts_down(parts):
    calls, *_ = parts
    d = make(parts)
    task = asyncio.create_task(d.run())
    await asyncio.wait_for(d.started.wait(), 1)
    os.kill(os.getpid(), signal.SIGINT)
    await asyncio.wait_for(task, 2)
    assert "orch.aclose" in calls


async def test_services_run_and_are_cancelled_at_shutdown(parts):
    ran = asyncio.Event()
    cancelled = []

    async def service():
        ran.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    d = make(parts, services=[service])
    task = await started(d)
    await asyncio.wait_for(ran.wait(), 1)
    d.stop()
    await task
    assert cancelled == [True]


async def test_mic_failure_is_retried_at_once_then_every_5s(parts):
    calls, audio, vad, ep, orch, log = parts
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)

    audio.fail_opens = 2
    d = make(parts, mic_retry_s=5.0, sleep=fake_sleep)
    task = await started(d)
    assert sleeps == [5.0]  # first retry immediately, the next one after 5 s
    assert audio.is_open  # third attempt worked
    assert calls.count("audio.open") == 3
    d.stop()
    await task


async def wait_for(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "condition not met"
        await asyncio.sleep(0.005)


async def test_stalled_mic_is_reopened_with_fresh_vad_and_endpointer(parts, caplog):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, mic_stall_s=0.05, mic_retry_s=0.0)
    task = await started(d)
    calls.clear()
    # no frame arrives (a USB drop: the callback just stops)
    await wait_for(lambda: "audio.open" in calls)
    i = calls.index("audio.close")
    assert calls[i:i + 4] == ["audio.close", "endpointer.reset", "vad.reset", "audio.open"]
    assert "no audio" in caplog.text
    audio.push(frame())
    await settle()
    assert vad.probs == 1  # reading again
    d.stop()
    await task


async def test_repeated_mic_failures_go_offline_until_frames_return(parts, caplog):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, mic_stall_s=0.03, mic_retry_s=0.0)
    task = await started(d)
    await wait_for(lambda: "orch.offline=True" in calls)
    assert calls.count("audio.open") >= 3
    assert "microphone unavailable" in caplog.text
    await wait_for(lambda: audio.is_open)
    audio.push(frame())
    await wait_for(lambda: "orch.offline=False" in calls)
    assert "microphone is back" in caplog.text
    d.stop()
    await task


async def test_offline_reasons_are_combined(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    d.set_offline("tts", True)
    d.set_offline("mic", True)
    d.set_offline("mic", False)
    assert calls[-1] == "orch.offline=True"  # VOICEVOX is still down
    assert d.offline_reasons == {"tts"}
    d.set_offline("tts", False)
    assert calls[-1] == "orch.offline=False"
    assert d.offline_reasons == set()


async def test_orchestrator_error_is_not_a_mic_failure(parts, caplog):
    calls, audio, vad, ep, orch, log = parts

    async def boom():
        raise ValueError("bug")

    orch.on_speech_started = boom
    d = make(parts)
    task = await started(d)
    calls.clear()
    audio.push(frame(0.9, [SpeechStarted()]))
    audio.push(frame())
    await settle()
    assert vad.probs == 2  # kept reading the same stream
    assert "audio.close" not in calls
    assert "speech start" in caplog.text and "microphone" not in caplog.text
    d.stop()
    await task


async def test_digital_silence_for_5s_reopens_the_mic_with_fresh_ears(parts, caplog):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, mic_retry_s=0.0)
    task = await started(d)
    calls.clear()
    zero = frame(0.0, level=0.0)
    for _ in range(155):  # 155 * 512 / 16000 = 4.96 s: not yet
        audio.push(zero)
    await settle(400)
    assert "audio.close" not in calls
    audio.push(zero)
    audio.push(zero)  # 5.0 s
    await wait_for(lambda: "audio.open" in calls)
    i = calls.index("audio.close")
    assert calls[i:i + 4] == ["audio.close", "endpointer.reset", "vad.reset", "audio.open"]
    assert "microphone delivers digital silence; reopening" in caplog.text
    assert d.debug()["zero_frames_streak_s"] == 0.0  # the streak starts over
    d.stop()
    await task


async def test_pause_and_resume_restart_the_silence_streak(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    zero = frame(0.0, level=0.0)
    for _ in range(125):  # 4 s
        audio.push(zero)
    await settle(300)
    await d.pause()
    await d.resume()
    assert d.debug()["zero_frames_streak_s"] == 0.0 and d.debug()["max_abs_last_5s"] is None
    calls.clear()
    for _ in range(62):  # 2 s
        audio.push(zero)
    await settle(300)
    assert "audio.close" not in calls
    d.stop()
    await task


async def test_a_signal_resets_the_silence_streak(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    calls.clear()
    zero = frame(0.0, level=0.0)
    for i in range(400):
        audio.push(frame(0.1, level=0.01) if i % 100 == 99 else zero)
    await settle(400)
    assert "audio.close" not in calls
    d.stop()
    await task


async def test_a_quiet_room_is_not_digital_silence(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    calls.clear()
    for _ in range(400):  # 12.8 s of near-silence, not exactly zero
        audio.push(frame(0.0, level=1e-7))
    await settle(400)
    assert "audio.close" not in calls
    info = d.debug()
    assert info["zero_frames_streak_s"] == 0.0 and 0 < info["max_abs_last_5s"] < 1e-6
    d.stop()
    await task


async def test_muted_ears_do_not_count_as_a_stalled_mic(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    d.set_ears_muted(True)
    calls.clear()
    for _ in range(400):
        audio.push(frame(0.0, level=0.0))
    await settle(400)
    assert "audio.close" not in calls
    d.stop()
    await task


async def test_persistent_digital_silence_goes_offline_and_a_signal_clears_it(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, mic_retry_s=0.0)
    task = await started(d)
    zero = frame(0.0, level=0.0)
    for _ in range(3):
        for _ in range(160):
            audio.push(zero)
        await settle(300)
        await wait_for(lambda: audio.is_open)
    assert "orch.offline=True" in calls
    audio.push(frame())
    await wait_for(lambda: "orch.offline=False" in calls)
    d.stop()
    await task


async def test_debug_reports_peak_and_zero_streak(parts):
    calls, audio, vad, ep, orch, log = parts
    clock = Clock()
    d = make(parts, clock=clock)
    task = await started(d)
    assert d.debug()["max_abs_last_5s"] is None
    audio.push(frame(level=0.25))
    audio.push(frame(0.0, level=0.0))
    audio.push(frame(0.0, level=0.0))
    await settle()
    info = d.debug()
    assert info["python"] == os.path.realpath(sys.executable)
    assert info["max_abs_last_5s"] == 0.25
    assert info["zero_frames_streak_s"] == round(1024 / 16000, 3)
    clock.t += 6  # the loud frame is out of the 5 s window
    assert d.debug()["max_abs_last_5s"] is None
    d.stop()
    await task


async def test_debug_reports_a_mic_fallback(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    assert d.debug()["mic_fallback"] is False
    audio.fallback = True
    assert d.debug()["mic_fallback"] is True


# ---- VOICEVOX reachability ----

class FakeVoicevox:
    def __init__(self):
        self.speaker_id = 0
        self.up = True
        self.pings = 0

    async def version(self):
        self.pings += 1
        if not self.up:
            raise httpx.ConnectError("refused")
        return "0.26.0"

    async def find_speaker(self, name, style="ノーマル"):
        if not self.up:
            raise httpx.ConnectError("refused")
        assert name == "青山龍星"
        return 13


async def test_watch_resolves_the_speaker_once_then_pings():
    vv = FakeVoicevox()
    flags = []
    w = TtsWatch(vv, flags.append, speaker_name="青山龍星")
    assert await w.check() is True
    assert vv.speaker_id == 13 and vv.pings == 0
    assert await w.check() is True
    assert vv.pings == 1
    assert flags == [False]  # reported on transitions only


async def test_watch_goes_offline_and_back():
    vv = FakeVoicevox()
    vv.up = False
    flags = []
    w = TtsWatch(vv, flags.append, speaker_name="青山龍星")
    assert await w.check() is False
    assert flags == [True] and vv.speaker_id == 0
    vv.up = True
    assert await w.check() is True
    assert flags == [True, False] and vv.speaker_id == 13


async def test_watch_retries_every_30s():
    vv = FakeVoicevox()
    vv.up = False
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    w = TtsWatch(vv, lambda _: None, sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await w.run()
    assert sleeps == [30.0, 30.0, 30.0] and vv.pings == 3


async def test_guarded_speak_marks_offline_when_voicevox_is_gone():
    flags = []
    w = TtsWatch(FakeVoicevox(), flags.append)

    async def speak(text):
        raise httpx.ConnectError("refused")

    assert await w.guard_speak(speak)("やあ。") is False
    assert flags == [True]


@pytest.mark.integration
async def test_watch_against_the_real_engine():
    from zundamon_jarvis.tts import DEFAULT_SPEAKER, Voicevox

    vv = Voicevox()
    flags = []
    try:
        w = TtsWatch(vv, flags.append, speaker_name=DEFAULT_SPEAKER)
        assert await w.check() is True and await w.check() is True
        assert vv.speaker_id == 13 and flags == [False]
    finally:
        await vv.aclose()


async def test_watch_retries_every_2s_until_the_speaker_is_resolved():
    vv = FakeVoicevox()
    vv.up = False
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            vv.up = True
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    w = TtsWatch(vv, lambda _: None, speaker_name="青山龍星", sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await w.run()
    assert sleeps == [2.0, 2.0, 30.0, 30.0]
    assert vv.speaker_id == 13


async def test_guarded_speak_never_speaks_with_an_unresolved_speaker():
    flags, spoken = [], []
    w = TtsWatch(FakeVoicevox(), flags.append, speaker_name="青山龍星")

    async def speak(text):
        spoken.append(text)
        return True

    assert await w.guard_speak(speak)("やあ。") is False
    assert spoken == [] and flags == [True]


async def test_guarded_synth_refuses_an_unresolved_speaker():
    from zundamon_jarvis.daemon import TtsUnavailable

    synthesized = []

    async def synth(text):
        synthesized.append(text)
        return np.zeros(10, np.float32)

    w = TtsWatch(FakeVoicevox(), lambda _: None, speaker_name="青山龍星")
    with pytest.raises(TtsUnavailable):
        await w.guard_synth(synth)("やあ。")  # a prefetch must not cache the wrong voice
    assert synthesized == []
    await w.check()
    assert (await w.guard_synth(synth)("やあ。")).shape == (10,)


async def test_a_successful_speak_clears_offline():
    flags = []
    w = TtsWatch(FakeVoicevox(), flags.append)
    calls = 0

    async def speak(text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return True

    guarded = w.guard_speak(speak)
    assert await guarded("a") is False
    assert await guarded("b") is True
    assert await guarded("c") is True
    assert flags == [True, False]  # only on the transition


# ---- startup ----

def test_already_running_exits_3_before_loading_anything(monkeypatch):
    import zundamon_jarvis.daemon as daemon_module

    async def must_not_run(*a, **kw):
        raise AssertionError("_amain must not run")

    monkeypatch.setattr(daemon_module.control, "is_live", lambda path: True)
    monkeypatch.setattr(daemon_module, "_amain", must_not_run)
    assert daemon_module.main([]) == daemon_module.EXIT_ALREADY_RUNNING == 3


def test_already_running_check_skips_the_heavy_imports(tmp_path):
    import socket
    import subprocess
    import sys

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(tmp_path / "zundamon-jarvis.sock"))
    srv.listen(1)
    try:
        code = ("import sys; from zundamon_jarvis.daemon import main; r = main([]); "
                "print(r, [m for m in ('faster_whisper', 'onnxruntime', 'sounddevice', "
                "'claude_agent_sdk') if m in sys.modules])")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}, timeout=30)
    finally:
        srv.close()
    assert out.stdout.strip() == "3 []", out.stderr
    assert "already running" in out.stderr


async def test_a_chosen_speaker_beats_a_lookup_already_in_flight():
    vv = FakeVoicevox()
    gate = asyncio.Event()
    real_find = vv.find_speaker

    async def slow_find(name, style="ノーマル"):
        await gate.wait()
        return await real_find(name, style)

    vv.find_speaker = slow_find
    w = TtsWatch(vv, lambda _: None, speaker_name="青山龍星")
    check = asyncio.create_task(w.check())
    await settle()
    w.choose(3)  # the user picked a voice while 青山龍星 was being looked up
    gate.set()
    assert await check is True
    assert vv.speaker_id == 3 and w.resolved


async def test_choose_before_any_lookup_skips_it():
    vv = FakeVoicevox()
    w = TtsWatch(vv, lambda _: None, speaker_name="青山龍星")
    w.choose(8)
    assert w.resolved
    await w.check()
    assert vv.speaker_id == 8 and vv.pings == 1  # a plain ping, no lookup


# ---- ears: preview mute safety and diagnostics ----

async def test_pause_and_resume_stop_a_preview_and_unmute(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    stopped = []
    d.interrupt_preview = lambda: stopped.append(1)
    task = await started(d)
    d.set_ears_muted(True)
    await d.pause()
    assert stopped == [1] and not d.ears_muted
    d.set_ears_muted(True)  # a preview while paused (allowed)
    await d.resume()
    assert stopped == [1, 1] and not d.ears_muted
    d.stop()
    await task


async def test_unmute_mid_utterance_tells_the_orchestrator(parts, caplog):
    import logging

    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    ep.in_speech = True
    orch.user_talking = True
    d.set_ears_muted(True)
    with caplog.at_level(logging.INFO, logger="zundamon_jarvis.daemon"):
        d.set_ears_muted(False)
    assert orch.abandoned == 1 and not ep.in_speech
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "ears unmuted" in msgs and "dropped (deaf)" in msgs


async def test_unmute_when_nothing_was_under_way_abandons_nothing(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    d.set_ears_muted(True)
    d.set_ears_muted(False)
    assert orch.abandoned == 0


async def test_mic_failure_mid_utterance_tells_the_orchestrator(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, mic_stall_s=0.05)
    task = await started(d)
    orch.user_talking = True
    ep.in_speech = True
    await wait_for(lambda: orch.abandoned >= 1)
    d.stop()
    await task


async def test_a_cut_utterance_resets_the_vad_and_warns(parts, caplog):
    import logging

    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    calls.clear()
    with caplog.at_level(logging.INFO, logger="zundamon_jarvis.daemon"):
        audio.push(frame(0.9, [Utterance(np.zeros(16000 * 30, np.float32), cut=True)]))
        await settle()
    assert "vad.reset" in calls and len(orch.utterances) == 1
    assert any(r.levelno == logging.WARNING and "cut" in r.getMessage() for r in caplog.records)
    d.stop()
    await task


async def test_speech_events_are_logged(parts, caplog):
    import logging

    calls, audio, vad, ep, orch, log = parts
    d = make(parts)
    task = await started(d)
    with caplog.at_level(logging.INFO, logger="zundamon_jarvis.daemon"):
        audio.push(frame(0.9, [SpeechStarted()]))
        audio.push(frame(0.1, [Utterance(np.zeros(16000, np.float32))]))
        await settle()
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "speech started" in msgs and "utterance 1.0 s" in msgs
    d.stop()
    await task


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def test_long_speech_and_long_user_talking_warn_once(parts, caplog):
    import logging

    calls, audio, vad, ep, orch, log = parts
    clock = Clock()
    d = make(parts, clock=clock)
    task = await started(d)
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    ep.in_speech = True
    orch.user_talking = True
    with caplog.at_level(logging.WARNING, logger="zundamon_jarvis.daemon"):
        audio.push(frame(0.9))
        await settle()
        clock.t += 31
        for _ in range(3):
            audio.push(frame(0.9))
            await settle()
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([w for w in warns if "in speech" in w]) == 1
    assert len([w for w in warns if "user_talking" in w]) == 1
    d.stop()
    await task


async def test_debug_reports_the_ears(parts):
    import json

    calls, audio, vad, ep, orch, log = parts
    clock = Clock()
    d = make(parts, clock=clock)
    task = await started(d)
    for p in (0.1, 0.3):
        audio.push(frame(p))
        await settle()
    clock.t += 1
    audio.push(frame(0.8, [SpeechStarted()]))
    await settle()
    ep.in_speech, ep.utterance_s = True, 0.5
    clock.t += 2.5
    info = d.debug()
    json.dumps(info)
    assert info["paused"] is False and info["ears_muted"] is False
    assert info["frames_last_5s"] == 3 and info["last_frame_age_s"] == 2.5
    assert info["vad"]["last_30s"] == {"n": 3, "max": 0.8, "mean": 0.4}
    assert info["vad"]["last_2s"] == {"n": 0, "max": None, "mean": None}
    assert info["endpointer"] == {"in_speech": True, "since_speech_started_s": 2.5,
                                  "utterance_s": 0.5}
    assert info["orchestrator"] == {"user_talking": False}
    assert info["offline_reasons"] == []
    d.stop()
    await task


async def test_debug_and_ears_reset_work_with_the_real_endpointer_and_orchestrator(parts):
    import json

    from fakes import FakeSpeaker

    from zundamon_jarvis.endpoint import Endpointer
    from zundamon_jarvis.orchestrator import Orchestrator

    calls, audio, vad, _, _, log = parts
    speaker = FakeSpeaker()
    orch = Orchestrator(lambda: None, transcribe=lambda a: "", speak=speaker.speak,
                        stop_speaking=speaker.stop)
    ep = Endpointer()
    d = Daemon(audio, vad, ep, orch, log)
    task = await started(d)
    for _ in range(10):  # 320 ms of speech: SpeechStarted, the user is mid-utterance
        audio.push(frame(0.9))
    await settle()
    assert ep.in_speech and orch.user_talking
    info = d.debug()
    json.dumps(info)
    assert info["endpointer"]["in_speech"] and info["orchestrator"]["user_talking"]
    d.set_ears_muted(True)
    d.set_ears_muted(False)  # the utterance is gone: the orchestrator must not keep waiting
    assert not ep.in_speech and not orch.user_talking
    d.stop()
    await task


# ---- half duplex (a Mac playing through its built-in speakers hears itself) ----

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def half(parts, **kw):
    clock = FakeClock()
    return make(parts, half_duplex=lambda: True, clock=clock, **kw), clock


async def test_half_duplex_drops_frames_while_speaking(parts):
    calls, audio, vad, ep, orch, log = parts
    d, clock = half(parts)
    task = await started(d)
    orch.saying = True
    for _ in range(3):
        audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert vad.probs == 0 and ep.fed == 0 and orch.started == 0  # no barge-in
    assert d.debug()["half_duplex_muted"] is True
    calls.clear()
    orch.saying = False
    audio.push(frame(0.9, [SpeechStarted()]))  # the echo tail: still dropped; the hold starts
    await settle()
    clock.t += HALF_DUPLEX_TAIL_S - 0.01
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert vad.probs == 0 and orch.started == 0 and calls == []
    clock.t += 0.02
    audio.push(frame(0.9, [SpeechStarted()]))
    audio.push(frame(0.9, []))
    await settle()
    assert calls == ["endpointer.reset", "vad.reset"]  # once, at the end of the hold
    assert vad.probs == 2 and ep.fed == 2 and orch.started == 1
    assert d.debug()["half_duplex_muted"] is False
    d.stop()
    await task


async def test_half_duplex_gap_between_sentences_keeps_the_mic_muted(parts):
    calls, audio, vad, ep, orch, log = parts
    asked = []
    clock = FakeClock()
    d = make(parts, half_duplex=lambda: asked.append(1) or True, clock=clock)
    task = await started(d)
    orch.saying = True
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    calls.clear()
    orch.saying = False  # the next streamed sentence is not in yet
    audio.push(frame())
    await settle()
    clock.t += HALF_DUPLEX_TAIL_S / 2
    audio.push(frame())
    await settle()
    orch.saying = True  # it is: speaking again within the hold
    audio.push(frame(0.9, [SpeechStarted()]))
    clock.t += HALF_DUPLEX_TAIL_S * 2
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert calls == [] and asked == [1]  # no reset, no re-check
    assert vad.probs == 0 and orch.started == 0
    d.stop()
    await task


async def test_pause_during_the_half_duplex_hold_clears_it(parts):
    calls, audio, vad, ep, orch, log = parts
    d, clock = half(parts)
    task = await started(d)
    orch.saying = True
    audio.push(frame())
    await settle()
    orch.saying = False
    audio.push(frame())  # the hold starts
    await settle()
    await d.pause()
    await d.resume()
    calls.clear()
    audio.push(frame(0.9, [SpeechStarted()]))  # straight away, well inside the old hold
    await settle()
    assert calls == [] and orch.started == 1  # no hold, no stray reset
    assert d.debug()["half_duplex_muted"] is False
    d.stop()
    await task


@pytest.mark.parametrize("saying_on_resume", [True, False])
async def test_pause_while_half_muted_then_resume(parts, saying_on_resume):
    calls, audio, vad, ep, orch, log = parts
    d, clock = half(parts)
    task = await started(d)
    orch.saying = True
    audio.push(frame())
    await settle()
    assert d.debug()["half_duplex_muted"] is True
    await d.pause()
    orch.saying = saying_on_resume
    await d.resume()
    calls.clear()
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert calls == []  # resume already reset the ears: no stray reset
    assert d.debug()["half_duplex_muted"] is saying_on_resume
    assert orch.started == (0 if saying_on_resume else 1)
    d.stop()
    await task


async def test_half_duplex_rechecks_the_output_each_time_speaking_starts(parts):
    calls, audio, vad, ep, orch, log = parts
    answers = [False, True]  # headphones first, then the built-in speakers
    asked = []

    def builtin():
        asked.append(1)
        return answers[len(asked) - 1]

    d = make(parts, half_duplex=builtin)
    task = await started(d)
    audio.push(frame())
    await settle()
    assert asked == []  # not speaking: nothing to ask
    orch.saying = True
    audio.push(frame(0.9, [SpeechStarted()]))
    audio.push(frame(0.9, []))
    await settle()
    assert asked == [1] and orch.started == 1  # headphones: full duplex, barge-in works
    orch.saying = False
    audio.push(frame())
    await settle()
    orch.saying = True
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert asked == [1, 1] and orch.started == 1  # speakers now: dropped
    d.stop()
    await task


async def test_half_duplex_dropped_frames_are_not_digital_silence(parts):
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, half_duplex=lambda: True)
    task = await started(d)
    orch.saying = True
    n = int(DIGITAL_SILENCE_S * SAMPLE_RATE / 512) + 20
    for _ in range(n):
        audio.push(frame(level=0.0))  # e.g. the input is gated while the output plays
    await settle()
    assert "audio.close" not in calls  # no reopen
    info = d.debug()
    assert info["zero_frames_streak_s"] == 0
    assert info["frames_last_5s"] == n  # dropped frames still count as arrived (watchdog)
    d.stop()
    await task


async def test_half_duplex_check_failure_keeps_full_duplex(parts):
    calls, audio, vad, ep, orch, log = parts

    def broken():
        raise OSError("CoreAudio")

    d = make(parts, half_duplex=broken)
    task = await started(d)
    orch.saying = True
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert orch.started == 1
    d.stop()
    await task


def test_duplex_auto_uses_builtin_speaker_detection():
    seen = []

    def builtin(system):
        seen.append(system)
        return True

    check = duplex_check("auto", "mac", builtin_speaker=builtin)
    assert check is not None and check() is True and seen == ["mac"]
    assert duplex_check("auto", "mac", builtin_speaker=lambda s: False)() is False
    assert duplex_check("half", "mac", builtin_speaker=builtin)() is True
    assert duplex_check("half", "linux", builtin_speaker=builtin)() is True
    assert duplex_check("full", "mac", builtin_speaker=builtin) is None
    assert seen == ["mac"]  # "half" / "full" never ask


def test_duplex_auto_on_windows_is_half_unless_the_output_is_headphones():
    seen = []

    def headphones(system):
        seen.append(system)
        return seen[-1] == "windows" and len(seen) > 1

    def no_mac(system):
        raise AssertionError("no Mac speaker detection on Windows")

    check = duplex_check("auto", "windows", builtin_speaker=no_mac, headphones=headphones)
    assert check is not None
    assert check() is True  # speakers (or unknown): half duplex
    assert check() is False  # headphones now: full duplex, asked again each time
    assert seen == ["windows", "windows"]
    assert duplex_check("full", "windows", headphones=headphones) is None
    assert duplex_check("half", "windows", headphones=lambda s: True)() is True
    assert duplex_check("auto", "linux", headphones=no_mac) is None
    assert duplex_check("auto", "mac", builtin_speaker=lambda s: False, headphones=no_mac)() is False


async def test_full_duplex_unchanged_on_linux(parts):
    def boom(system):
        raise AssertionError("no speaker detection on Linux")

    assert duplex_check("auto", "linux", builtin_speaker=boom) is None
    assert duplex_check("full", "linux", builtin_speaker=boom) is None
    calls, audio, vad, ep, orch, log = parts
    d = make(parts, half_duplex=duplex_check("auto", "linux", builtin_speaker=boom))
    task = await started(d)
    orch.saying = True
    audio.push(frame(0.9, [SpeechStarted()]))
    await settle()
    assert vad.probs == 1 and orch.started == 1  # barge-in while speaking, as before
    calls.clear()
    orch.saying = False
    audio.push(frame())
    await settle()
    assert calls == []  # no extra resets
    d.stop()
    await task


def test_stuck_audio_stops_the_daemon_once_and_arms_a_hard_exit():
    stops, timers = [], []

    class FakeTimer:
        def __init__(self, after, fn, args=()):
            self.after, self.fn, self.args = after, fn, args
            self.daemon = False
            timers.append(self)

        def start(self):
            self.started = True

    exits = []
    guard = AudioGuard(lambda: stops.append(1), hard_exit=exits.append, timer=FakeTimer)
    assert guard.exit_code == 0
    guard("playback did not finish")
    guard("again")  # the second player too: still one restart
    assert stops == [1] and len(timers) == 1
    (t,) = timers
    assert t.daemon and t.started and t.after == HARD_EXIT_S
    t.fn(*t.args)  # the clean shutdown hung too
    assert exits == [EXIT_AUDIO_STUCK] and guard.exit_code == EXIT_AUDIO_STUCK != 0
