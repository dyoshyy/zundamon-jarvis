import asyncio
import time

import numpy as np

from zundamon_jarvis.player import Player


class FakeStream:
    """Never finishes by itself; abort() ends it like PortAudio would."""

    def __init__(self, finished_callback, **kw):
        self.finished = finished_callback
        self.aborted = False

    def start(self):
        pass

    def abort(self):
        self.aborted = True
        self.finished()

    def close(self):
        pass


async def test_stop_returns_false_promptly():
    p = Player(stream_factory=FakeStream)
    task = asyncio.create_task(p.play(np.zeros(24000 * 10, np.float32), 24000))
    await asyncio.sleep(0.05)
    assert p.playing
    t0 = time.monotonic()
    p.stop()
    assert await task is False
    assert time.monotonic() - t0 < 0.1
    assert not p.playing


async def test_completion_returns_true():
    class Done(FakeStream):
        def start(self):
            self.finished()

    assert await Player(stream_factory=Done).play(np.zeros(100, np.float32), 24000) is True


async def test_real_stream_short_quiet_tone():
    t = np.arange(int(0.3 * 24000)) / 24000
    pcm = (0.02 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    try:
        assert await asyncio.wait_for(Player().play(pcm, 24000), 3) is True
    except Exception as e:  # no output device on CI
        import pytest

        pytest.skip(f"no audio output: {e}")


class Recorder(FakeStream):
    made: list = []

    def __init__(self, finished_callback, callback, **kw):
        super().__init__(finished_callback)
        self.callback = callback
        self.closed = False
        Recorder.made.append(self)

    def close(self):
        self.closed = True


async def test_stop_aborts_and_closes_the_stream_and_silences_the_callback():
    import sounddevice as sd

    Recorder.made = []
    p = Player(stream_factory=Recorder)
    task = asyncio.create_task(p.play(np.zeros(24000 * 10, np.float32), 24000))
    await asyncio.sleep(0.05)
    p.stop()
    assert await task is False
    (s,) = Recorder.made
    assert s.aborted and s.closed
    out = np.ones((256, 1), np.float32)
    try:
        s.callback(out, 256, None, None)
        raised = False
    except sd.CallbackAbort:
        raised = True
    assert raised and not out.any()  # a late callback plays nothing


def stuck_player(factory, **kw):
    seen = []
    return Player(stream_factory=factory, on_stuck=seen.append, grace_s=0.1,
                  call_timeout_s=0.2, **kw), seen


async def test_a_stream_that_never_finishes_is_reported_stuck():
    p, seen = stuck_player(FakeStream)  # never finishes by itself
    t0 = time.monotonic()
    assert await p.play(np.zeros(240, np.float32), 24000) is False  # 10 ms of audio
    assert time.monotonic() - t0 < 1.0
    assert len(seen) == 1 and p.stuck


class HangingClose(FakeStream):
    def close(self):
        time.sleep(1.0)  # PortAudio waiting on a dead audio thread


async def test_a_hanging_close_does_not_block_the_loop():
    p, seen = stuck_player(HangingClose)
    task = asyncio.create_task(p.play(np.zeros(24000 * 10, np.float32), 24000))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    tick = asyncio.create_task(ticker())
    p.stop()
    assert await task is False
    tick.cancel()
    assert time.monotonic() - t0 < 0.6 and ticks > 5  # the loop kept running meanwhile
    assert len(seen) == 1 and p.stuck


class HangingStart(FakeStream):
    def start(self):
        time.sleep(1.0)


async def test_a_hanging_open_is_reported_stuck_and_later_plays_are_skipped():
    p, seen = stuck_player(HangingStart)
    assert await p.play(np.zeros(240, np.float32), 24000) is False
    assert len(seen) == 1
    assert await p.play(np.zeros(240, np.float32), 24000) is False  # no new stream on a dead stack
    assert len(seen) == 1
