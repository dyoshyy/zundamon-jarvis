"""Test doubles for the orchestrator: a scripted brain, a fake speaker and a fake clock."""

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator

from zundamon_jarvis.brain import BrainEvent, TurnEnd


async def settle(rounds: int = 50) -> None:
    """Let every ready task run until the loop goes quiet."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    """Epoch-seconds clock whose sleeps only finish when the test advances time."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._now + seconds, next(self._seq), fut))
        await fut

    async def advance(self, seconds: float) -> None:
        target = self._now + seconds
        await settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, fut = heapq.heappop(self._sleepers)
            self._now = max(self._now, deadline)
            if not fut.done():
                fut.set_result(None)
            await settle()
        self._now = target
        await settle()


class FakeBrain:
    """Plays a script: each submit() emits the next list of events from ``script``."""

    def __init__(self, script: list[list[BrainEvent]] | None = None,
                 interrupt_ends_turn: bool = True) -> None:
        self.script = list(script or [])
        self.interrupt_ends_turn = interrupt_ends_turn
        self.busy = False
        self.background_running = False
        self.submitted: list[str] = []
        self.interrupts = 0
        self.closed = False
        self.close_started = False
        self.close_cancelled = False
        self.close_gate: asyncio.Event | None = None  # set it to make close() slow
        self._queue: asyncio.Queue[BrainEvent] = asyncio.Queue()
        self._consumers = 0

    def emit(self, *events: BrainEvent) -> None:
        for ev in events:
            if isinstance(ev, TurnEnd):
                self.busy = False
            self._queue.put_nowait(ev)

    async def submit(self, text: str) -> None:
        self.submitted.append(text)
        self.busy = True
        if self.script:
            self.emit(*self.script.pop(0))

    async def events(self) -> AsyncIterator[BrainEvent]:
        self._consumers += 1
        assert self._consumers == 1, "events() must have a single consumer"
        while True:
            yield await self._queue.get()

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self.interrupt_ends_turn and self.busy:
            self.emit(TurnEnd())

    async def close(self) -> None:
        self.close_started = True
        try:
            if self.close_gate is not None:
                await self.close_gate.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.closed = True
        self.busy = False


class BrainFactory:
    def __init__(self, *brains: FakeBrain) -> None:
        self._brains = list(brains)
        self.made: list[FakeBrain] = []

    def __call__(self) -> FakeBrain:
        brain = self._brains.pop(0) if self._brains else FakeBrain()
        self.made.append(brain)
        return brain


class FakeSpeaker:
    """speak() finishes at once, or (block=True) waits until finish() or stop()."""

    def __init__(self, block: bool = False) -> None:
        self.block = block
        self.spoken: list[str] = []
        self.stops = 0
        self.prefetched: list[str] = []
        self._current: asyncio.Future[bool] | None = None

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        if not self.block:
            await asyncio.sleep(0)
            return True
        self._current = asyncio.get_running_loop().create_future()
        try:
            return await self._current
        finally:
            self._current = None

    def finish(self) -> None:
        if self._current is not None and not self._current.done():
            self._current.set_result(True)

    def stop(self) -> None:
        self.stops += 1
        if self._current is not None and not self._current.done():
            self._current.set_result(False)

    def prefetch(self, text: str) -> None:
        self.prefetched.append(text)
