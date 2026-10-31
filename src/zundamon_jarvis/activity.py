"""What the conversation watch window shows: daemon and orchestrator signals -> watch events.

The daemon wires its hooks here (heard transcripts, the orchestrator's ``on_say`` /
``on_confirm`` / ``on_brain_event``, state changes) and the feed publishes compact events to
the control socket's ``WatchHub``:

- ``heard {text}``, ``said {text, what}`` (what: reply / filler / question / notice)
- ``tool {label, labels, sub}``: one English line from ``labels.tool_label``, never the input;
  ``labels`` is that line in every display language (``{"ja": ..., "en": ...}``: the window
  picks its own, so the history follows a language switch); ``sub`` is true for a subagent's
  tool use
- ``confirm {question, ok, why}`` (why: answer / unclear / timeout)
- ``state {state, text[, reasons]}`` (``control.status_of``; the window labels it with
  ``control.status_label``): only paused / limit / offline and the return to listening;
  thinking / speaking would flood the window
- ``thinking {on}``: the brain started / stopped working on a turn (the window shows a spinner,
  never a line)
- ``task {status, summary}``: a background task ended
- ``hearing {on}``: the user's speech is being taken in (the daemon's SpeechStarted until the
  utterance ends); live only (``publish_live``), never in the history
"""

import logging
from collections.abc import Callable, Iterable
from typing import Any

from zundamon_jarvis.brain import TaskDone, ToolUse
from zundamon_jarvis.config import LANGUAGES
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
        publish_live: Callable[..., Any] | None = None,
    ) -> None:
        self._publish = publish
        self._publish_live = publish if publish_live is None else publish_live
        self._reasons = offline_reasons
        self._mode: str | None = None
        self._thinking = False

    def _emit(self, kind: str, **fields: Any) -> None:
        self._send(self._publish, kind, fields)

    def _send(self, publish: Callable[..., Any], kind: str, fields: dict[str, Any]) -> None:
        try:
            publish(kind, **fields)
        except Exception:
            log.exception("watch publish failed")

    def hearing(self, on: bool) -> None:
        self._send(self._publish_live, "hearing", {"on": bool(on)})

    def heard(self, text: str) -> None:
        self._emit("heard", text=text)

    def said(self, text: str, what: str) -> None:
        self._emit("said", text=text, what=what)

    def confirm(self, question: str, ok: bool, why: str) -> None:
        self._emit("confirm", question=question, ok=bool(ok), why=why)

    def brain_event(self, ev: Any) -> None:
        if isinstance(ev, ToolUse):
            try:
                labels = {lang: tool_label(ev.name, ev.input, lang) for lang in LANGUAGES}
            except Exception:
                log.exception("tool_label failed for %s", ev.name)
                labels = {lang: f"🔧 {ev.name}" for lang in LANGUAGES}
            self._emit("tool", label=labels["en"], labels=labels, sub=ev.agent_id is not None)
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
            status = status_of(mode, self._reasons())
        except Exception:
            status = {"state": mode, "text": mode}
        self._emit("state", **{k: v for k, v in status.items() if k in ("state", "text", "reasons")})
