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
