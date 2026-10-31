"""Microphone input (the OS default input by default; or a configured mic name / device index).

Device resolution: the PortAudio build here has only the ALSA and JACK host
APIs (no native PulseAudio one). The ALSA "pulse" device talks to
pipewire-pulse and honours the PULSE_SOURCE environment variable, which takes
the exact PipeWire node name (e.g. "zundamon_jarvis_aec_source"). So a string device
name that is not already a PortAudio device index is resolved by setting
PULSE_SOURCE and opening "pulse"; an int is passed to PortAudio unchanged. None
(no mic_device configured) opens "pulse" without setting PULSE_SOURCE, so the stream
follows the OS default input (PipeWire's default source).

macOS (CoreAudio): PULSE_SOURCE is never touched. None opens sounddevice's default input; a
name is a CoreAudio input device name, resolved to its PortAudio index by exact match
(sounddevice's own name lookup matches substrings and gives up on two hits); an int is an index.

Windows: the same as macOS, with MME devices (see ``zundamon_jarvis.mics`` for why MME). None
opens sounddevice's default input, which is MME's Sound Mapper (MME is PortAudio's default host
API on Windows), so it follows the Windows default recording device and resamples to 16 kHz;
a name is an MME input device name; an int is any PortAudio index (one of another host API
that will not open at 16 kHz falls back to the default, like a missing mic).

``close()`` never waits on PortAudio: aborting and closing the stream happen on a thread.
When the audio server goes away (PipeWire restarted) the ALSA backend's capture thread can
spin forever and that abort never returns; done on the event loop it froze the daemon.
``open()`` waits up to ``close_timeout_s`` for the previous stream to close (PortAudio calls
stay one at a time); if it is still not closed the input is *stuck*: ``on_stuck(why)`` is
called once (the daemon restarts itself) and every open fails.
"""

import asyncio
import logging
import os
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np
import sounddevice as sd

from zundamon_jarvis import platform
from zundamon_jarvis.diag import RateLimitedLog
from zundamon_jarvis.mics import input_device_index, installed_source_names

log = logging.getLogger(__name__)

FRAME_SAMPLES = 512
# Linux: set mic_device = "zundamon_jarvis_aec_source" in config.toml to use echo cancellation
# (see deploy/). On a Mac mic_device is a CoreAudio input device name, on Windows an MME one.
_MAX_BUFFERED_FRAMES = 500  # ~16 s; the oldest frames are dropped beyond this
CLOSE_TIMEOUT_S = 5.0


class AudioIn:
    def __init__(
        self,
        device: str | int | None = None,
        rate: int = 16000,
        stream_factory: Callable[..., Any] = sd.InputStream,
        source_names: Callable[[], set[str] | None] | None = None,
        *,
        system: str | None = None,
        input_index: Callable[[str], int | None] | None = None,
        on_stuck: Callable[[str], None] = lambda why: None,
        close_timeout_s: float = CLOSE_TIMEOUT_S,
    ) -> None:
        self._on_stuck = on_stuck
        self._close_timeout_s = close_timeout_s
        self._closing: threading.Thread | None = None
        self.stuck = False
        self._system = system or platform.system()
        if source_names is None:
            sys_ = self._system
            source_names = lambda: installed_source_names(system=sys_)
        if input_index is None:
            sys_ = self._system
            input_index = lambda name: input_device_index(name, system=sys_)
        self._input_index = input_index  # macOS / Windows: device name -> PortAudio index
        self._device = device
        self._rate = rate
        self._factory = stream_factory
        self._stream: Any = None
        self._fallback = False
        self._source_names = source_names  # None = cannot tell (no pactl): just try to open
        self._rlog = RateLimitedLog(log, per_window=1, window_s=60.0)
        self._lock = threading.Lock()
        self._frames: deque[np.ndarray] = deque(maxlen=_MAX_BUFFERED_FRAMES)
        self._pending = np.zeros(0, np.float32)
        self._gen = 0  # bumped by close(); stale callbacks and iterators check it
        self._wake: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None

    # --- lifecycle -------------------------------------------------------

    @property
    def fallback(self) -> bool:
        """True while a configured mic could not be opened and the OS default is in use."""
        return self._fallback

    def wait_closed(self, timeout: float) -> bool:
        """True once the last closed stream is really closed (False: still not, after timeout)."""
        closing = self._closing
        if closing is not None:
            closing.join(timeout)
            if closing.is_alive():
                return False
            self._closing = None
        return True

    def open(self) -> None:
        if self._stream is not None:
            return
        if self.stuck:
            raise RuntimeError("audio input stuck: the previous stream never closed")
        if not self.wait_closed(self._close_timeout_s):
            self.stuck = True
            why = f"closing the previous input stream did not finish in {self._close_timeout_s:g} s"
            log.error("audio input stuck: %s", why)
            try:
                self._on_stuck(why)
            except Exception:
                log.exception("on_stuck failed")
            raise RuntimeError(f"audio input stuck: {why}")
        with self._lock:
            self._frames.clear()
            self._pending = np.zeros(0, np.float32)
            gen = self._gen

        def callback(indata, frames, time, status):  # audio thread
            self._feed(gen, indata[:, 0])

        # pipewire-pulse silently serves the default for an unknown PULSE_SOURCE, so a named
        # source is checked against the installed ones first.
        names = self._source_names() if isinstance(self._device, str) else None
        if names is not None and self._device not in names:
            self._rlog(logging.WARNING, "mic-missing",
                       "microphone %r is not available; falling back to the system default input",
                       self._device)
            stream = self._start(None, callback)
            self._fallback = True
        else:
            try:
                stream = self._start(self._device, callback)
                self._fallback = False
            except Exception as e:
                if self._device is None:
                    raise
                self._rlog(logging.WARNING, "mic-missing",
                           "microphone %r could not be opened (%s); falling back to the "
                           "system default input", self._device, e)
                stream = self._start(None, callback)
                self._fallback = True
        self._stream = stream

    def _start(self, device_cfg: str | int | None, callback: Callable[..., None]) -> Any:
        if self._system in ("mac", "windows"):  # no PipeWire: PortAudio devices directly
            device: str | int | None = device_cfg
            if isinstance(device_cfg, str):
                device = self._input_index(device_cfg)
                if device is None:
                    raise LookupError(f"no input device named {device_cfg!r}")
            return self._open_stream(device, callback)
        saved = os.environ.get("PULSE_SOURCE")
        try:
            if isinstance(device_cfg, str):
                os.environ["PULSE_SOURCE"] = device_cfg
                device = "pulse"
            else:
                device = "pulse" if device_cfg is None else device_cfg
            return self._open_stream(device, callback)
        finally:  # PULSE_SOURCE is only needed while the stream connects
            if saved is None:
                os.environ.pop("PULSE_SOURCE", None)
            else:
                os.environ["PULSE_SOURCE"] = saved

    def _open_stream(self, device: str | int | None, callback: Callable[..., None]) -> Any:
        stream = self._factory(
            device=device,
            samplerate=self._rate,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            callback=callback,
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream

    def close(self) -> None:
        """Stop reading immediately; buffered audio is discarded."""
        with self._lock:
            self._gen += 1
            self._frames.clear()
            self._pending = np.zeros(0, np.float32)
        stream, self._stream = self._stream, None
        if stream is not None:
            self._closing = threading.Thread(target=_stop_stream, args=(stream,), daemon=True,
                                             name="audio-in-close")
            self._closing.start()
        self._notify()

    # --- audio thread ----------------------------------------------------

    def _feed(self, gen: int, samples: np.ndarray) -> None:
        with self._lock:
            if gen != self._gen:
                return
            buf = np.concatenate([self._pending, np.asarray(samples, np.float32)])
            n = len(buf) // FRAME_SAMPLES
            for i in range(n):
                self._frames.append(buf[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES].copy())
            self._pending = buf[n * FRAME_SAMPLES :].copy()
        if n:
            self._notify()

    def _notify(self) -> None:
        wake = self._wake
        if wake is not None:
            loop, event = wake
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # loop already closed
                pass

    def _buffered(self) -> int:
        with self._lock:
            return len(self._frames)

    # --- consumer --------------------------------------------------------

    async def frames(self) -> AsyncIterator[np.ndarray]:
        """Yield 512-sample float32 mono frames until close() is called."""
        if self._stream is None:
            self.open()
        gen = self._gen
        event = asyncio.Event()
        self._wake = (asyncio.get_running_loop(), event)
        try:
            while True:
                with self._lock:
                    if gen != self._gen:
                        return
                    frame = self._frames.popleft() if self._frames else None
                if frame is not None:
                    yield frame
                    continue
                event.clear()
                with self._lock:  # re-check so a feed between pop and clear is not lost
                    if gen != self._gen:
                        return
                    if self._frames:
                        continue
                await event.wait()
        finally:
            if self._wake is not None and self._wake[1] is event:
                self._wake = None


def _stop_stream(stream: Any) -> None:
    try:
        stream.abort()
    except Exception:
        log.debug("input stream abort failed", exc_info=True)
    try:
        stream.close()
    except Exception:
        log.debug("input stream close failed", exc_info=True)
