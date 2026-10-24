"""Conversation log: ``~/.local/state/zundamon-jarvis/YYYY-MM-DD.jsonl``, one line per utterance.

Each line is ``{"ts", "heard", "replied", "state"}``: when the utterance was heard, its
transcript, everything ZundamonJarvis said in reply (sentences joined), and the daemon state at the
moment it was heard. A line is written when the orchestrator goes quiet again after working
on it (thinking/speaking -> anything else), when the next utterance arrives, or at shutdown.
Speech with nothing heard before it (a background task's report) gets a line with
``heard: ""``. Files older than 14 days are removed at startup (``prune``). Logs left in the
pre-rename ``~/.local/state/jarvis`` are moved here at startup (``migrate_legacy_logs``).
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from zundamon_jarvis import platform

log = logging.getLogger(__name__)

KEEP_DAYS = 14
_BUSY = ("thinking", "speaking")


def _state_home() -> Path:
    return platform.state_home()


def log_dir() -> Path:
    return platform.log_dir()


def legacy_log_dir() -> Path:
    """Where the logs were kept before the project was renamed."""
    return _state_home() / "jarvis"


def migrate_legacy_logs(directory: Path, legacy: Path) -> list[Path]:
    """Move the pre-rename YYYY-MM-DD.jsonl files into ``directory``; returns the new paths.

    A day already present in ``directory`` is left where it is (never overwritten). The
    legacy directory is removed once it is empty.
    """
    if not legacy.is_dir():
        return []
    moved = []
    for p in sorted(legacy.glob("*.jsonl")):
        dest = directory / p.name
        if dest.exists():
            log.warning("not migrating %s: %s already exists", p, dest)
            continue
        directory.mkdir(parents=True, exist_ok=True)
        shutil.move(p, dest)
        moved.append(dest)
    try:
        legacy.rmdir()
    except OSError:
        pass  # not empty: something we did not move stays for the user to look at
    return moved


def prune(directory: Path, today: date, keep_days: int = KEEP_DAYS) -> list[Path]:
    """Delete YYYY-MM-DD.jsonl files dated ``keep_days`` or more days before today."""
    if not directory.is_dir():
        return []
    cutoff = today - timedelta(days=keep_days)
    removed = []
    for p in directory.glob("*.jsonl"):
        try:
            day = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if day <= cutoff:
            p.unlink()
            removed.append(p)
    return removed


class ConversationLog:
    def __init__(
        self,
        directory: Path,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._dir = directory
        self._now = now
        self._cur: dict[str, Any] | None = None
        self._busy = False  # the orchestrator has worked on the current entry

    def heard(self, text: str, state: str) -> None:
        self.flush()
        self._start(text, state)

    def replied(self, text: str) -> None:
        if self._cur is None:
            self._start("", "speaking")
        assert self._cur is not None
        self._cur["replied"].append(text)

    def on_state(self, state: str) -> None:
        if self._cur is None:
            return
        if state in _BUSY:
            self._busy = True
        elif self._busy or self._cur["replied"]:
            self.flush()

    def flush(self) -> None:
        cur, self._cur = self._cur, None
        if cur is None:
            return
        ts: datetime = cur["ts"]
        line = {
            "ts": ts.isoformat(timespec="seconds"),
            "heard": cur["heard"],
            "replied": "".join(cur["replied"]),
            "state": cur["state"],
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._dir / f"{ts.date().isoformat()}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("could not write the conversation log")

    def close(self) -> None:
        self.flush()

    def _start(self, heard: str, state: str) -> None:
        self._cur = {"ts": self._now(), "heard": heard, "replied": [], "state": state}
        self._busy = False


def logged_transcribe(
    transcribe: Callable[[Any], str],
    loop: asyncio.AbstractEventLoop,
    on_heard: Callable[[str], None],
) -> Callable[[Any], str]:
    """Wrap the (thread-run) transcriber so each result is reported on the event loop.

    The callback is scheduled before the function returns, so it runs before the coroutine
    awaiting ``to_thread`` resumes: the log sees "heard" before the orchestrator reacts.
    """

    def wrapped(audio: Any) -> str:
        text = transcribe(audio)
        loop.call_soon_threadsafe(on_heard, text)
        return text

    return wrapped


def logged_speak(
    speak: Callable[[str], Awaitable[bool]],
    on_reply: Callable[[str], None],
) -> Callable[[str], Awaitable[bool]]:
    async def wrapped(text: str) -> bool:
        on_reply(text)
        return await speak(text)

    return wrapped
