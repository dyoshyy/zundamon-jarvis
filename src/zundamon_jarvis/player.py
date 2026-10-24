"""Interruptible audio playback to the default output (the echo-cancel sink)."""

import asyncio
from collections.abc import Callable
from typing import Any

import numpy as np
import sounddevice as sd


class Player:
    def __init__(self, stream_factory: Callable[..., Any] = sd.OutputStream) -> None:
        self._factory = stream_factory
        self._stream: Any = None
        self._fut: asyncio.Future[bool] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.playing = False

    def _resolve(self, fut: asyncio.Future[bool], value: bool) -> None:
        if not fut.done():
            fut.set_result(value)

    async def play(self, pcm: np.ndarray, rate: int) -> bool:
        """True if played to the end, False if stop() interrupted it."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        data = np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1, 1)
        pos = 0

        def callback(outdata, frames, time, status):  # audio thread
            nonlocal pos
            chunk = data[pos : pos + frames]
            n = len(chunk)
            outdata[:n] = chunk
            outdata[n:] = 0
            pos += n
            if n < frames:
                raise sd.CallbackStop

        def finished():  # audio thread
            loop.call_soon_threadsafe(self._resolve, fut, True)

        stream = self._factory(
            samplerate=rate,
            channels=1,
            dtype="float32",
            callback=callback,
            finished_callback=finished,
        )
        self._stream, self._fut, self._loop = stream, fut, loop
        self.playing = True
        try:
            stream.start()
            return await fut
        finally:
            self.playing = False
            self._stream = self._fut = None
            try:
                stream.close()
            except Exception:
                pass

    def stop(self) -> None:
        stream, fut, loop = self._stream, self._fut, self._loop
        if fut is None or loop is None:
            return
        loop.call_soon_threadsafe(self._resolve, fut, False)
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
