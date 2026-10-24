import asyncio
import os

import numpy as np
import pytest

from zundamon_jarvis.audio_in import AudioIn


class FakeInputStream:
    """Captures the callback so tests can push audio like the PortAudio thread would."""

    instances: list["FakeInputStream"] = []

    def __init__(self, **kw):
        self.kw = kw
        self.callback = kw["callback"]
        self.started = False
        self.closed = False
        FakeInputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def abort(self):
        self.started = False

    def close(self):
        self.closed = True

    def push(self, samples):
        self.callback(np.asarray(samples, np.float32).reshape(-1, 1), len(samples), None, None)


@pytest.fixture(autouse=True)
def _reset():
    FakeInputStream.instances = []


def make(**kw):
    return AudioIn(stream_factory=FakeInputStream, **kw)


async def take(it, n):
    return [await asyncio.wait_for(it.__anext__(), 1) for _ in range(n)]


async def test_frames_are_512_float32_mono():
    a = make()
    a.open()
    it = a.frames()
    FakeInputStream.instances[-1].push(np.arange(1024))
    f1, f2 = await take(it, 2)
    assert f1.shape == (512,) and f1.dtype == np.float32
    assert f1[0] == 0 and f2[0] == 512
    a.close()


async def test_partial_blocks_are_accumulated():
    a = make()
    a.open()
    it = a.frames()
    s = FakeInputStream.instances[-1]
    s.push(np.ones(300))
    s.push(np.ones(300) * 2)
    (f,) = await take(it, 1)
    assert np.all(f[:300] == 1) and np.all(f[300:] == 2)
    a.close()


async def test_close_stops_delivery_and_drops_buffered_audio():
    a = make()
    a.open()
    it = a.frames()
    s = FakeInputStream.instances[-1]
    s.push(np.zeros(2048))  # 4 frames buffered
    await take(it, 1)
    a.close()
    assert s.closed and not s.started
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(it.__anext__(), 1)
    s.push(np.zeros(1024))  # late callback after close: ignored
    assert a._buffered() == 0


async def test_close_wakes_a_waiting_consumer():
    a = make()
    a.open()
    it = a.frames()
    waiter = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.01)
    a.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiter, 1)


async def test_reopen_starts_fresh():
    a = make()
    a.open()
    FakeInputStream.instances[-1].push(np.ones(700))  # 188 samples left over
    a.close()
    a.open()
    it = a.frames()
    s2 = FakeInputStream.instances[-1]
    assert s2 is not FakeInputStream.instances[0]
    s2.push(np.full(512, 7.0))
    (f,) = await take(it, 1)
    assert np.all(f == 7.0)
    a.close()


async def test_open_twice_is_idempotent_and_close_without_open_is_safe():
    a = make()
    a.close()
    a.open()
    a.open()
    assert len(FakeInputStream.instances) == 1
    a.close()


async def test_stream_parameters_and_pulse_device_resolution(monkeypatch):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    seen_env = []

    class Spy(FakeInputStream):
        def __init__(self, **kw):
            seen_env.append(os.environ.get("PULSE_SOURCE"))
            super().__init__(**kw)

    a = AudioIn(device="zundamon_jarvis_aec_source", rate=16000, stream_factory=Spy, source_names=lambda: None)
    a.open()
    kw = FakeInputStream.instances[-1].kw
    assert kw["samplerate"] == 16000 and kw["channels"] == 1 and kw["dtype"] == "float32"
    assert kw["device"] == "pulse"
    assert seen_env == ["zundamon_jarvis_aec_source"]  # set while the stream was created
    assert "PULSE_SOURCE" not in os.environ  # and removed afterwards
    a.close()


async def test_pulse_source_is_restored_even_if_factory_raises(monkeypatch):
    monkeypatch.setenv("PULSE_SOURCE", "other")

    def boom(**kw):
        raise RuntimeError("no device")

    a = AudioIn(stream_factory=boom)
    with pytest.raises(RuntimeError):
        a.open()
    assert os.environ["PULSE_SOURCE"] == "other"


async def test_default_device_follows_the_os_default_input(monkeypatch):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    seen = []

    class Spy(FakeInputStream):
        def __init__(self, **kw):
            seen.append(os.environ.get("PULSE_SOURCE"))
            super().__init__(**kw)

    a = AudioIn(stream_factory=Spy)
    a.open()
    assert seen == [None]  # no PULSE_SOURCE: pipewire-pulse picks its default source
    kw = FakeInputStream.instances[-1].kw
    assert kw["device"] == "pulse" and kw["samplerate"] == 16000 and kw["channels"] == 1
    a.close()


async def test_integer_device_is_passed_through():
    a = make(device=3)
    a.open()
    assert FakeInputStream.instances[-1].kw["device"] == 3
    a.close()


@pytest.mark.integration
async def test_real_aec_source_delivers_frames():
    a = AudioIn(source_names=lambda: None)
    a.open()
    try:
        f = await asyncio.wait_for(a.frames().__anext__(), 2)
        assert f.shape == (512,)
    finally:
        a.close()


async def test_a_missing_named_mic_falls_back_to_the_os_default(monkeypatch, caplog):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    seen = []

    def factory(**kw):
        seen.append((kw["device"], os.environ.get("PULSE_SOURCE")))
        if os.environ.get("PULSE_SOURCE") == "gone":
            raise OSError("source not found")
        return FakeInputStream(**kw)

    a = AudioIn(device="gone", stream_factory=factory, source_names=lambda: None)
    assert a.fallback is False
    a.open()
    assert seen == [("pulse", "gone"), ("pulse", None)]
    assert a.fallback is True
    assert "WARNING" in [r.levelname for r in caplog.records]
    assert "gone" in caplog.text and "system default" in caplog.text
    assert "PULSE_SOURCE" not in os.environ
    assert FakeInputStream.instances[-1].started
    a.close()


async def test_the_named_mic_is_tried_again_on_every_open(monkeypatch):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    present = {"cam": False}

    def factory(**kw):
        if os.environ.get("PULSE_SOURCE") == "cam" and not present["cam"]:
            raise OSError("gone")
        return FakeInputStream(**kw)

    a = AudioIn(device="cam", stream_factory=factory, source_names=lambda: None)
    a.open()
    assert a.fallback is True
    a.close()
    present["cam"] = True  # plugged back in
    a.open()
    assert a.fallback is False
    a.close()


async def test_a_failing_default_mic_still_raises():
    def boom(**kw):
        raise OSError("no mic at all")

    a = AudioIn(device="cam", stream_factory=boom, source_names=lambda: None)
    with pytest.raises(OSError):
        a.open()
    assert a.fallback is False


async def test_a_named_mic_not_listed_by_pactl_falls_back_without_trying_it(monkeypatch, caplog):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    seen = []

    def factory(**kw):
        seen.append(os.environ.get("PULSE_SOURCE"))
        return FakeInputStream(**kw)

    a = AudioIn(device="gone", stream_factory=factory, source_names=lambda: {"alsa_input.cam"})
    a.open()
    assert seen == [None] and a.fallback is True  # pipewire-pulse would have served the default silently
    assert "gone" in caplog.text
    a.close()


async def test_a_listed_named_mic_is_used(monkeypatch):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    seen = []

    def factory(**kw):
        seen.append(os.environ.get("PULSE_SOURCE"))
        return FakeInputStream(**kw)

    a = AudioIn(device="cam", stream_factory=factory, source_names=lambda: {"cam"})
    a.open()
    assert seen == ["cam"] and a.fallback is False
    a.close()


async def test_the_missing_mic_warning_is_rate_limited(caplog):
    a = AudioIn(device="gone", stream_factory=FakeInputStream, source_names=lambda: set())
    for _ in range(4):
        a.open()
        a.close()
    assert caplog.text.count("not available") == 1
    assert a.fallback is True


# ---- macOS (CoreAudio through sounddevice; no PipeWire) ----

async def test_mac_open_does_not_touch_pulse_source(monkeypatch):
    monkeypatch.setenv("PULSE_SOURCE", "left-alone")
    seen = []

    class Spy(FakeInputStream):
        def __init__(self, **kw):
            seen.append((kw["device"], os.environ.get("PULSE_SOURCE")))
            super().__init__(**kw)

    a = AudioIn(system="mac", stream_factory=Spy)  # mic_device None: the default input
    a.open()
    a.close()
    b = AudioIn(device="USB Audio Device", system="mac", stream_factory=Spy,
                source_names=lambda: {"USB Audio Device"},
                input_index=lambda name: 2 if name == "USB Audio Device" else None)
    b.open()
    b.close()
    assert seen == [(None, "left-alone"), (2, "left-alone")]  # never "pulse"
    assert os.environ["PULSE_SOURCE"] == "left-alone"
    assert not b.fallback


async def test_mac_missing_named_mic_falls_back_to_the_default_input(caplog):
    a = AudioIn(device="gone", system="mac", stream_factory=FakeInputStream,
                source_names=lambda: {"MacBook Pro Microphone"}, input_index=lambda name: None)
    a.open()
    assert FakeInputStream.instances[-1].kw["device"] is None and a.fallback
    assert "not available" in caplog.text
    a.close()


async def test_mac_named_mic_that_vanishes_before_opening_falls_back():
    a = AudioIn(device="cam", system="mac", stream_factory=FakeInputStream,
                source_names=lambda: None, input_index=lambda name: None)
    a.open()
    assert [s.kw["device"] for s in FakeInputStream.instances] == [None] and a.fallback
    a.close()
