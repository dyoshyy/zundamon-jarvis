"""Small diagnostics helpers: rate-limited log lines and sliding windows of recent values.

Used by the daemon's ``debug`` command (``jarvisctl debug``) and its INFO lines for speech
events, to find out where the ears get stuck when it happens again.
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class RateLimitedLog:
    """At most ``per_window`` lines per key per ``window_s``; then one "N suppressed" line."""

    def __init__(self, logger: logging.Logger, per_window: int = 6, window_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._log = logger
        self._per = per_window
        self._window = window_s
        self._clock = clock
        self._state: dict[str, list] = {}  # key -> [window start, count, suppressed]

    def __call__(self, level: int, key: str, msg: str, *args: Any) -> None:
        now = self._clock()
        st = self._state.get(key)
        if st is None or now - st[0] >= self._window:
            if st is not None and st[2]:
                self._log.log(level, "%s: %d similar lines suppressed", key, st[2])
            st = self._state[key] = [now, 0, 0]
        if st[1] < self._per:
            st[1] += 1
            self._log.log(level, msg, *args)
        else:
            st[2] += 1


class Window:
    """(time, value) pairs from the last ``span_s`` seconds."""

    def __init__(self, span_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._span = span_s
        self._clock = clock
        self._items: deque[tuple[float, float]] = deque()
        self._last: float | None = None

    def add(self, value: float) -> None:
        now = self._clock()
        self._last = now
        self._items.append((now, value))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self._span:
            self._items.popleft()

    def _recent(self, last_s: float | None) -> list[float]:
        now = self._clock()
        self._trim(now)
        span = self._span if last_s is None else last_s
        return [v for t, v in self._items if now - t <= span]

    def clear(self) -> None:
        self._items.clear()

    def count(self, last_s: float | None = None) -> int:
        return len(self._recent(last_s))

    def stats(self, last_s: float | None = None) -> dict:
        vals = self._recent(last_s)
        if not vals:
            return {"n": 0, "max": None, "mean": None}
        return {"n": len(vals), "max": round(max(vals), 3), "mean": round(sum(vals) / len(vals), 3)}

    def peak(self, last_s: float | None = None) -> float | None:
        """The largest value, unrounded (stats() rounds to 3 places: a quiet room would read 0)."""
        vals = self._recent(last_s)
        return max(vals) if vals else None

    def last_age(self) -> float | None:
        return None if self._last is None else round(self._clock() - self._last, 3)
