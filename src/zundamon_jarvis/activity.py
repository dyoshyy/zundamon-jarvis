"""What the conversation watch window shows: daemon and orchestrator signals -> watch events.

The daemon wires its hooks here (heard transcripts, the orchestrator's ``on_say`` /
``on_confirm`` / ``on_brain_event``, state changes) and the feed publishes compact events to
the control socket's ``WatchHub``:

- ``heard {text}``, ``said {text, what}`` (what: reply / filler / question / notice)
- ``tool {label, sub}``: one English line from ``labels.tool_label``, never the input;
  ``sub`` is true for a subagent's tool use
- ``confirm {question, ok, why}`` (why: answer / unclear / timeout)
- ``state {state, text}``: only paused / limit / offline and the return to listening;
  thinking / speaking would flood the window
- ``thinking {on}``: the brain started / stopped working on a turn (the window shows a spinner,
  never a line)
- ``task {status, summary}``: a background task ended
"""

import logging
from collections.abc import Callable, Iterable
from typing import Any

from zundamon_jarvis.brain import TaskDone, ToolUse
from zundamon_jarvis.control import status_of
from zundamon_jarvis.labels import tool_label

log = logging.getLogger(__name__)

SUMMARY_MAX = 60
_QUIET_STATES = ("paused", "limit", "offline")


class ActivityFeed:
    def __init__(
        self,
        publish: Callable[..., Any],
        offline_reasons: Callable[[], Iterable[str]] = lambda: (),
    ) -> None:
        self._publish = publish
        self._reasons = offline_reasons
        self._mode: str | None = None
        self._thinking = False

    def _emit(self, kind: str, **fields: Any) -> None:
        try:
            self._publish(kind, **fields)
        except Exception:
            log.exception("watch publish failed")

    def heard(self, text: str) -> None:
        self._emit("heard", text=text)

    def said(self, text: str, what: str) -> None:
        self._emit("said", text=text, what=what)

    def confirm(self, question: str, ok: bool, why: str) -> None:
        self._emit("confirm", question=question, ok=bool(ok), why=why)

    def brain_event(self, ev: Any) -> None:
        if isinstance(ev, ToolUse):
            try:
                label = tool_label(ev.name, ev.input)
            except Exception:
                log.exception("tool_label failed for %s", ev.name)
                label = f"🔧 {ev.name}"
            self._emit("tool", label=label, sub=ev.agent_id is not None)
        elif isinstance(ev, TaskDone):
            summary = " ".join((ev.summary or "").split())
            if len(summary) > SUMMARY_MAX:
                summary = summary[: SUMMARY_MAX - 1] + "…"
            self._emit("task", status=ev.status, summary=summary)

    def state(self, state: str) -> None:
        if (state == "thinking") != self._thinking:
            self._thinking = state == "thinking"
            self._emit("thinking", on=self._thinking)
        mode = state if state in _QUIET_STATES else "listening"
        if mode == self._mode:
            return
        self._mode = mode
        try:
            text = status_of(mode, self._reasons())["text"]
        except Exception:
            text = mode
        self._emit("state", state=mode, text=text)
