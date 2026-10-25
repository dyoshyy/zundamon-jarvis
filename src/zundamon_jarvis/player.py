"""Interruptible audio playback to the default output (the echo-cancel sink).

PortAudio calls that wait on the audio thread (opening, aborting, closing a stream) run on a
worker thread with a time limit, never on the event loop: when the audio server goes away
mid-stream (PipeWire restarted) PortAudio's ALSA backend can spin forever, and a blocking
``abort()`` there froze the whole daemon. Such a stream, or one that keeps going
``grace_s`` past the end of its audio, makes the player *stuck*: ``on_stuck(why)`` is called
once (the daemon restarts itself) and nothing is played any more.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

GRACE_S = 5.0  # a stream still going this long after its audio ended is stuck
CALL_TIMEOUT_S = 5.0  # opening / stopping a stream takes longer than this: stuck


class _Stuck(Exception):
    pass


class Player:
    def __init__(
        self,
        stream_factory: Callable[..., Any] = sd.OutputStream,
        on_stuck: Callable[[str], None] = lambda why: None,
        grace_s: float = GRACE_S,
        call_timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._factory = stream_factory
        self._on_stuck = on_stuck
        self._grace_s = grace_s
        self._call_timeout_s = call_timeout_s
        self._fut: asyncio.Future[bool] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped: threading.Event | None = None
        self.playing = False
        self.stuck = False

    def _resolve(self, fut: asyncio.Future[bool], value: bool) -> None:
        if not fut.done():
            fut.set_result(value)

    def _stick(self, why: str) -> None:
        if self.stuck:
            return
        self.stuck = True
        log.error("audio output stuck: %s", why)
        try:
            self._on_stuck(why)
        except Exception:
            log.exception("on_stuck failed")

    async def _call(self, fn: Callable[[], Any], what: str) -> Any:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), self._call_timeout_s)
        except TimeoutError:
            self._stick(f"{what} did not return in {self._call_timeout_s:g} s")
            raise _Stuck from None

    async def play(self, pcm: np.ndarray, rate: int) -> bool:
        """True if played to the end, False if stop() interrupted it (or the output is stuck)."""
        if self.stuck:
            return False
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        stopped = threading.Event()
        data = np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1, 1)
        pos = 0

        def callback(outdata, frames, time, status):  # audio thread
            nonlocal pos
            if stopped.is_set():
                outdata[:] = 0
                raise sd.CallbackAbort
            chunk = data[pos : pos + frames]
            n = len(chunk)
            outdata[:n] = chunk
            outdata[n:] = 0
            pos += n
            if n < frames:
                raise sd.CallbackStop

        def finished():  # audio thread
            loop.call_soon_threadsafe(self._resolve, fut, True)

        def open_() -> Any:
            stream = self._factory(
                samplerate=rate,
                channels=1,
                dtype="float32",
                callback=callback,
                finished_callback=finished,
            )
            stream.start()
            return stream

        stream = None
        self._fut, self._loop, self._stopped = fut, loop, stopped
        self.playing = True
        try:
            stream = await self._call(open_, "opening the output stream")
            try:
                return await asyncio.wait_for(fut, len(data) / rate + self._grace_s)
            except TimeoutError:
                self._stick(f"playback did not finish {self._grace_s:g} s after the audio ended")
                return False
        except _Stuck:
            return False
        finally:
            self.playing = False
            self._fut = self._stopped = None
            stopped.set()
            if stream is not None and not self.stuck:
                try:
                    await self._call(lambda: _stop_stream(stream), "stopping the output stream")
                except _Stuck:
                    pass

    def stop(self) -> None:
        """Interrupt the current play() (it returns False); never waits on PortAudio."""
        fut, loop, stopped = self._fut, self._loop, self._stopped
        if fut is None or loop is None:
            return
        if stopped is not None:
            stopped.set()
        loop.call_soon_threadsafe(self._resolve, fut, False)


def _stop_stream(stream: Any) -> None:
    try:
        stream.abort()  # already finished: nothing to abort
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass
