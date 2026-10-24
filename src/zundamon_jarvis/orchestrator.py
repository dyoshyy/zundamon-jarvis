"""Turn-taking: what the daemon says, when, and what it sends to Claude.

Everything external is injected: the brain (``brain_factory``), transcription, speech and the
clock. The daemon feeds endpointer events in (``on_speech_started`` / ``on_utterance``) and the
hotkey (``pause`` / ``resume``).

How it behaves:

- Replies are spoken sentence by sentence as they stream in. ``[[silent]]``, markdown and
  punctuation-only fragments are never spoken. If a reply has no sentence after 3 s, one filler.
- Nothing is spoken while the user is talking (between SpeechStarted and Utterance); it waits.
- Barge-in: the user starting to talk over a reply (or over the filler) stops it, drops the
  rest of the reply, and
  interrupts Claude - except while any confirmation is pending, because ``interrupt()`` denies
  the main agent's pending request; then only the voice stops.
- An utterance while Claude is busy is queued and sent after the turn ends. Our own turn
  tracking (submits vs TurnEnds) decides, not only ``brain.busy``.
- Tool confirmations are asked one at a time; the next utterance answers. Unclear twice ->
  deny and send that utterance as a normal message. No answer in 20 s -> deny (the clock
  stops while the user is talking).
- Pause: stop talking. Replies already under way stay silent (the work itself goes on);
  background reports and confirmations that arrive meanwhile wait for resume.
- Usage limit: say so once, then stay quiet and send nothing until the reset (or 1 h).
- 30 min without an utterance and no background work -> close the session; the next
  utterance starts a new brain.
- Watching (the conversation window): ``on_brain_event`` sees every brain event (tool uses
  and background-task ends are only shown, never spoken), ``on_say(text, kind)`` each thing
  as it starts being said, ``on_confirm(question, ok, why)`` how a confirmation ended
  (``why``: ``answer`` / ``unclear`` / ``timeout``). A failing hook is logged and ignored.
- Settings window: ``user_talking`` / ``saying`` / ``has_session`` are read-only views (a voice
  sample is never played over either); ``restart_session()`` closes an idle session at once
  (same check as the idle close) so the next utterance starts a brain with the new model.
"""

import asyncio
import logging
import re
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import numpy as np

from zundamon_jarvis.brain import (
    AuthProblem,
    Brain,
    BrainEvent,
    PermissionRequest,
    TaskDone,
    TextDelta,
    ToolUse,
    TurnEnd,
    UsageLimit,
)
from zundamon_jarvis.diag import RateLimitedLog
from zundamon_jarvis.policy import describe_action, parse_confirmation
from zundamon_jarvis.text import SILENT_TOKEN, SentenceChunker, is_hallucination, speakable

log = logging.getLogger(__name__)
_drop_log = RateLimitedLog(log)

State = Literal["listening", "paused", "thinking", "speaking", "limit", "offline"]

# Spoken lines are conversation, not UI: Japanese for Japanese speech, English otherwise.
FILLERS = ["ちょっと待ってね", "ちょっと調べるね"]
FILLERS_EN = ["One moment", "Let me check"]
LIMIT_NOTICE = "使用量の上限に達したから、しばらく黙るね"
LIMIT_NOTICE_EN = "I've hit my usage limit, so I'll stay quiet for a while"
_FALLBACK_QUESTION = "この操作をするけど、いい？"
_FALLBACK_QUESTION_EN = "I'm about to do this. Is that okay?"
_JAPANESE_CHARS = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _is_japanese(text: str) -> bool:
    return bool(_JAPANESE_CHARS.search(text))
_MIN_LIMIT_WAIT_S = 60.0  # a reset time already in the past must not unmute us at once


@dataclass(frozen=True)
class OrchestratorConfig:
    filler_after_s: float = 3.0
    confirm_timeout_s: float = 20.0
    idle_close_s: float = 1800.0
    idle_recheck_s: float = 60.0  # while background work keeps the session open
    limit_retry_s: float = 3600.0  # when the limit gives no reset time


class Clock(Protocol):
    def now(self) -> float: ...  # epoch seconds
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(eq=False)
class _Turn:
    solicited: bool  # False: an unsolicited turn (a background task's report)
    muted: bool = False
    chunker: SentenceChunker = field(default_factory=SentenceChunker)
    head: str = ""  # first characters, to spot a [[silent]] that is still streaming
    has_speech: bool = False  # produced a sentence worth saying
    spoke: bool = False  # one of its sentences has started playing
    ended: bool = False


@dataclass(eq=False)
class _Confirmation:
    req: PermissionRequest
    question: str
    queued: bool = False  # question is in the speech queue
    asked: bool = False  # question has started playing: the next utterance answers it
    unclear: int = 0
    deadline: float = 0.0  # when the running timeout fires
    remaining: float | None = None  # timeout left while suspended (the user is talking)


@dataclass(eq=False)
class _Say:
    text: str
    kind: Literal["reply", "filler", "question", "notice"]
    turn: _Turn | None = None
    confirm: _Confirmation | None = None
    prefetched: bool = False


class Orchestrator:
    def __init__(
        self,
        brain_factory: Callable[[], Brain],
        transcribe: Callable[[Any], str],
        speak: Callable[[str], Awaitable[bool]],
        stop_speaking: Callable[[], None],
        clock: Clock | None = None,
        config: OrchestratorConfig | None = None,
        *,
        prefetch: Callable[[str], None] | None = None,
    ) -> None:
        self._factory = brain_factory
        self._transcribe = transcribe
        self._speak = speak
        self._stop = stop_speaking
        self._prefetch = prefetch
        self._clock: Clock = clock or SystemClock()
        self._config = config or OrchestratorConfig()

        self.state: State = "listening"
        self.on_state_change: Callable[[State], None] = lambda state: None
        self.on_brain_event: Callable[[BrainEvent], None] = lambda ev: None
        self.on_say: Callable[[str, str], None] = lambda text, kind: None
        self.on_confirm: Callable[[str, bool, str], None] = lambda question, ok, why: None

        self._brain: Brain | None = None
        self._pump: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None  # an old brain being closed
        self._closed = False
        self._paused = False
        self._limited = False
        self._auth_seen = False  # an AuthProblem arrived in the turn under way
        self.login_ok = True  # False after an authentication failure, until the next good turn
        self._offline = False
        self._user_talking = False
        self._japanese = True  # language of the last utterance (STT is Japanese by default)

        self._inputs: deque[str] = deque()  # transcripts waiting to be sent
        self._outstanding: deque[_Turn] = deque()  # sent, no event seen yet
        self._turn: _Turn | None = None  # the turn the brain is streaming now
        self._confirms: deque[_Confirmation] = deque()  # head is the one being asked

        self._speech: deque[_Say] = deque()
        self._saying: _Say | None = None
        self._speech_wake = asyncio.Event()
        self._speech_task: asyncio.Task | None = None

        self._confirm_timer: asyncio.Task | None = None
        self._idle_timer: asyncio.Task | None = None
        self._limit_timer: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- inputs from the daemon ----

    async def on_speech_started(self) -> None:
        if self._paused or self._closed:
            return
        self._user_talking = True
        self._suspend_confirm_timer()
        answering = bool(self._confirms) and self._confirms[0].asked
        item = self._saying
        if item is not None:
            self._stop_voice()
            if item.kind == "reply" and item.turn is not None and not answering:
                self._barge_in(item.turn)
            elif item.kind == "filler" and item.turn is not None and not self._confirms:
                self._barge_in(item.turn)  # the answer to what they talked over is stale
        elif self._turn is not None and self._turn.spoke and not answering:
            self._barge_in(self._turn)  # between two sentences of a reply
        self._refresh()

    async def on_utterance(self, audio: np.ndarray | Any) -> None:
        if self._paused or self._closed:
            self._dropped("paused" if self._paused else "closed")
            return
        self._user_talking = False
        self._wake()  # held speech may go now
        try:
            await self._hear(audio)
        finally:
            # The confirmation timeout stays suspended until the utterance has been handled.
            if not self._user_talking:
                self._resume_confirm_timer()
            self._refresh()

    async def _hear(self, audio: np.ndarray | Any) -> None:
        try:
            text = await asyncio.to_thread(self._transcribe, audio)
        except Exception:
            log.exception("transcription failed")
            return
        text = (text or "").strip()
        why = ("paused" if self._paused else "closed" if self._closed else "empty" if not text
               else "hallucination" if is_hallucination(text) else None)
        if why is not None:
            self._dropped(why, text)
            return
        self._touch()
        self._japanese = _is_japanese(text)
        if not self._answer(text):
            if self._limited:
                self._dropped("limit", text)
                return
            self._inputs.append(text)
            self._dispatch()

    @staticmethod
    def _dropped(why: str, text: str = "") -> None:
        _drop_log(logging.INFO, f"drop-{why}", "utterance dropped (%s)%s", why,
                  f" ({len(text)} chars)" if text else "")

    def on_speech_abandoned(self) -> None:
        """The user's speech ended without an Utterance (the ears were reset mid-utterance):
        stop holding speech for them, and restart a confirmation clock suspended for them."""
        if not self._user_talking:
            return
        self._user_talking = False
        self._wake()
        self._resume_confirm_timer()
        self._refresh()

    def debug_state(self) -> dict:
        """For ``jarvisctl debug``: plain JSON-able values only."""
        t = self._turn
        b = self._brain
        return {
            "user_talking": self._user_talking,
            "saying": self._saying.kind if self._saying is not None else None,
            "speech_queue": len(self._speech),
            "confirms": len(self._confirms),
            "confirm_asked": bool(self._confirms) and self._confirms[0].asked,
            "queued_inputs": len(self._inputs),
            "outstanding_turns": len(self._outstanding),
            "turn": None if t is None else {"solicited": t.solicited, "muted": t.muted,
                                            "spoke": t.spoke, "ended": t.ended,
                                            "has_speech": t.has_speech},
            "paused": self._paused,
            "limited": self._limited,
            "offline": self._offline,
            "has_session": b is not None,
            "brain_busy": bool(b is not None and b.busy),
            "background_running": bool(b is not None and b.background_running),
        }

    async def pause(self) -> None:
        if self._paused:
            return
        self._paused = True
        self._user_talking = False
        self._stop_voice()
        # Whatever was being said or thought about goes quiet; the work itself continues.
        for turn in (self._turn, *self._outstanding):
            if turn is not None and (turn.solicited or turn.spoke):
                turn.muted = True
        if self._saying is not None and self._saying.turn is not None:
            self._saying.turn.muted = True
        # Confirmations are asked again after resume.
        self._cancel(self._confirm_timer)
        for c in self._confirms:
            c.queued = c.asked = False
            c.remaining = None
        self._speech = deque(i for i in self._speech if i.kind not in ("filler", "question"))
        self._refresh()

    async def resume(self) -> None:
        if not self._paused or self._closed:
            return
        self._paused = False
        self._ask_next()
        self._wake()
        self._refresh()

    def set_offline(self, offline: bool) -> None:
        self._offline = offline
        self._refresh()

    # ---- for the settings window (read-only, plus a session restart) ----

    @property
    def user_talking(self) -> bool:
        """Between SpeechStarted and the Utterance: the user is mid-utterance."""
        return self._user_talking

    @property
    def saying(self) -> bool:
        """Something (a reply, a filler, a question, a notice) is being said right now."""
        return self._saying is not None

    @property
    def has_session(self) -> bool:
        return self._brain is not None

    def restart_session(self) -> Literal["closed", "none", "busy"]:
        """Close the session now if it is idle (as the 30-min idle close would); the next
        utterance starts a new brain (with the model configured by then)."""
        if self._brain is None:
            return "none"
        if not self._can_close():
            return "busy"
        log.info("closing the session on request (settings)")
        self._cancel(self._idle_timer)
        self._close_brain()
        self._refresh()
        return "closed"

    # ---- the brain ----

    def _ensure_brain(self) -> Brain:
        if self._brain is None:
            brain = self._factory()
            self._brain = brain
            self._auth_seen = False  # a new session: nothing stale from the old one
            self._pump = asyncio.create_task(self._pump_events(brain))
        return self._brain

    async def _pump_events(self, brain: Brain) -> None:
        try:
            async for ev in brain.events():
                if brain is not self._brain:
                    return
                self._notify(self.on_brain_event, ev)
                try:
                    self._on_event(ev)
                except Exception:
                    log.exception("failed to handle %s", type(ev).__name__)  # not its text
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("brain event stream failed")
        if brain is self._brain:  # the stream ended on its own: start over on the next utterance
            self._brain = None
            self._outstanding.clear()
            self._turn = None
            self._confirms.clear()  # the brain has denied them already
            self._cancel(self._confirm_timer)
            self._refresh()

    def _on_event(self, ev: BrainEvent) -> None:
        if isinstance(ev, (ToolUse, TaskDone)):
            return  # for the watch window only (on_brain_event); nothing to say or track
        if isinstance(ev, AuthProblem):
            self._auth_seen = True  # status shows it (login_ok); nothing to say
            self.login_ok = False
            return
        if isinstance(ev, TextDelta) and not self._auth_seen:
            self.login_ok = True
        if isinstance(ev, TurnEnd):
            if ev.ok and not self._auth_seen:
                self.login_ok = True  # a successful turn, even without text
            self._auth_seen = False
        try:
            if isinstance(ev, TextDelta):
                self._on_text(ev.text)
            elif isinstance(ev, TurnEnd):
                self._on_turn_end()
            elif isinstance(ev, UsageLimit):
                self._on_limit(ev.reset_at)
            elif isinstance(ev, PermissionRequest):
                self._on_permission(ev)
        finally:  # a failure above must not strand the queued utterances
            self._dispatch()
            self._refresh()

    def _current_turn(self) -> _Turn:
        if self._turn is None:
            if self._outstanding:
                self._turn = self._outstanding.popleft()
            else:
                self._turn = _Turn(solicited=False, muted=self._limited)
        return self._turn

    def _on_text(self, text: str) -> None:
        turn = self._current_turn()
        if len(turn.head) < len(SILENT_TOKEN):
            turn.head += text
        if turn.muted:
            return
        for sentence in turn.chunker.feed(text):
            self._say_sentence(turn, sentence)

    def _on_turn_end(self) -> None:
        turn = self._turn
        if turn is None and self._outstanding:
            turn = self._outstanding.popleft()  # a turn with no text (tools only, or empty)
        self._turn = None
        if turn is None:
            return
        turn.ended = True
        if not turn.muted:
            for sentence in turn.chunker.flush():
                self._say_sentence(turn, sentence)

    def _say_sentence(self, turn: _Turn, sentence: str) -> None:
        text = speakable(sentence.replace(SILENT_TOKEN, ""))
        if not any(ch.isalnum() for ch in text):  # empty, or a stray "？" / "」"
            return
        turn.has_speech = True
        self._enqueue(_Say(text, "reply", turn=turn))

    def _dispatch(self) -> None:
        """Send the next queued utterance if no turn is under way."""
        if not self._inputs:
            return
        if self._limited:
            self._inputs.clear()
            return
        if self._outstanding or self._turn is not None:
            return
        if self._brain is not None and self._brain.busy:
            return
        text = self._inputs.popleft()
        turn = _Turn(solicited=True, muted=self._paused)
        self._outstanding.append(turn)
        self._spawn(self._submit(turn, text))
        if not turn.muted:
            self._spawn(self._filler_after(turn))

    async def _submit(self, turn: _Turn, text: str) -> None:
        try:
            if self._closing is not None:
                await asyncio.shield(self._closing)  # a fresh brain only after the old one is gone
            brain = self._ensure_brain()
            await brain.submit(text)
        except Exception:
            log.exception("submit failed")
            if turn in self._outstanding:
                self._outstanding.remove(turn)
            turn.ended = True
            self._dispatch()
            self._refresh()

    async def _filler_after(self, turn: _Turn) -> None:
        await self._clock.sleep(self._config.filler_after_s)
        if (turn.has_speech or turn.ended or turn.muted or self._paused or self._limited
                or self._user_talking or self._confirms):
            return
        head = turn.head.strip()
        if head and SILENT_TOKEN.startswith(head):
            return
        self._enqueue(_Say(random.choice(FILLERS if self._japanese else FILLERS_EN), "filler", turn=turn))

    def _barge_in(self, turn: _Turn) -> None:
        turn.muted = True
        # interrupt() denies the main agent's pending confirmation, so never while one is open.
        if not turn.ended and not self._confirms and self._brain is not None:
            self._spawn(self._interrupt(self._brain))

    async def _interrupt(self, brain: Brain) -> None:
        try:
            await brain.interrupt()
        except Exception:
            log.exception("interrupt failed")

    # ---- confirmations ----

    def _on_permission(self, req: PermissionRequest) -> None:
        try:
            question = describe_action(req.tool, req.input)
        except Exception:
            log.exception("describe_action failed for %s", req.tool)
            question = _FALLBACK_QUESTION if self._japanese else _FALLBACK_QUESTION_EN
        self._confirms.append(_Confirmation(req, question))
        self._ask_next()

    def _ask_next(self) -> None:
        if not self._confirms or self._paused:
            return
        c = self._confirms[0]
        if not c.queued and not c.asked:
            c.queued = True
            self._enqueue(_Say(c.question, "question", confirm=c))

    def _answer(self, text: str) -> bool:
        """Treat the utterance as the answer to an open question. False: send it on."""
        c = self._confirms[0] if self._confirms else None
        if c is None or not c.asked:
            return False
        verdict = parse_confirmation(text)
        if verdict != "unclear":
            self._resolve(c, verdict == "yes", "answer")
            return True
        if c.unclear == 0:
            c.unclear = 1
            c.asked = False
            c.remaining = None
            self._cancel(self._confirm_timer)
            self._ask_next()
            return True
        self._resolve(c, False, "unclear")
        return False

    def _resolve(self, c: _Confirmation, ok: bool, why: str) -> None:
        self._cancel(self._confirm_timer)
        self._notify(self.on_confirm, c.question, ok, why)
        if self._confirms and self._confirms[0] is c:
            self._confirms.popleft()
        try:
            c.req.decide(ok)
        except Exception:
            log.exception("decide failed")
        self._ask_next()

    def _question_done(self, c: _Confirmation) -> None:
        if self._paused or not self._confirms or self._confirms[0] is not c or not c.asked:
            return
        if self._user_talking:  # they are already answering: the clock starts when they stop
            self._cancel(self._confirm_timer)
            c.remaining = self._config.confirm_timeout_s
            return
        self._start_confirm_timer(c, self._config.confirm_timeout_s)

    def _start_confirm_timer(self, c: _Confirmation, seconds: float) -> None:
        self._cancel(self._confirm_timer)
        c.remaining = None
        c.deadline = self._clock.now() + seconds
        self._confirm_timer = self._spawn(self._confirm_timeout(c, seconds))

    def _suspend_confirm_timer(self) -> None:
        c = self._confirms[0] if self._confirms else None
        timer = self._confirm_timer
        if c is None or not c.asked or timer is None or timer.done():
            return
        self._cancel(timer)
        c.remaining = max(c.deadline - self._clock.now(), 0.0)

    def _resume_confirm_timer(self) -> None:
        c = self._confirms[0] if self._confirms else None
        if c is None or not c.asked or c.remaining is None or self._paused:
            return
        self._start_confirm_timer(c, c.remaining)

    async def _confirm_timeout(self, c: _Confirmation, seconds: float) -> None:
        await self._clock.sleep(seconds)
        if self._confirms and self._confirms[0] is c:
            log.info("no answer to the confirmation (%d chars); denying", len(c.question))
            self._resolve(c, False, "timeout")
            self._refresh()

    # ---- usage limit ----

    def _on_limit(self, reset_at: datetime | None) -> None:
        if not self._limited:
            self._limited = True
            self._enqueue(_Say(LIMIT_NOTICE if self._japanese else LIMIT_NOTICE_EN, "notice"))
        for turn in (self._turn, *self._outstanding):
            if turn is not None:
                turn.muted = True
        self._inputs.clear()
        if reset_at is None:
            delay = self._config.limit_retry_s
        else:
            delay = max(reset_at.timestamp() - self._clock.now(), _MIN_LIMIT_WAIT_S)
        self._cancel(self._limit_timer)
        self._limit_timer = self._spawn(self._lift_limit(delay))

    async def _lift_limit(self, delay: float) -> None:
        await self._clock.sleep(delay)
        self._limited = False
        self._refresh()

    # ---- idle session close ----

    def _touch(self) -> None:
        self._cancel(self._idle_timer)
        self._idle_timer = self._spawn(self._idle_watch())

    def _can_close(self) -> bool:
        b = self._brain
        return not (b.busy or b.background_running or self._outstanding or self._turn
                    or self._inputs or self._confirms or self._saying)

    async def _idle_watch(self) -> None:
        await self._clock.sleep(self._config.idle_close_s)
        while self._brain is not None and not self._can_close():
            await self._clock.sleep(self._config.idle_recheck_s)
        if self._brain is not None:
            log.info("idle: closing the session")
            self._close_brain()

    def _close_brain(self) -> asyncio.Task | None:
        """Close the current brain in its own task, so cancelling a timer can't cut it short."""
        brain, pump = self._brain, self._pump
        self._brain = self._pump = None
        if brain is None:
            return self._closing
        previous = self._closing

        async def close() -> None:
            try:
                if previous is not None:
                    await asyncio.shield(previous)
                await brain.close()  # disconnect() fires the SessionEnd capture hook
            except Exception:
                log.exception("close failed")
            finally:
                if pump is not None:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
                if self._closing is task:
                    self._closing = None

        task = asyncio.create_task(close())
        self._closing = task
        return task

    async def aclose(self) -> None:
        """Shut down for good: stop talking, cancel everything, close the session (to the end)."""
        self._closed = True
        self._stop_voice()
        for c in self._confirms:
            try:
                c.req.decide(False)
            except Exception:
                log.exception("decide failed")
        self._confirms.clear()
        tasks = [t for t in (self._confirm_timer, self._idle_timer, self._limit_timer,
                             self._speech_task, *self._tasks) if t is not None]
        for t in tasks:
            t.cancel()
        closing = self._close_brain()  # started first: it runs to the end even if we are cancelled
        await asyncio.gather(*tasks, return_exceptions=True)
        if closing is not None:
            await asyncio.shield(closing)
        self._refresh()

    # ---- speech ----

    def _enqueue(self, item: _Say) -> None:
        self._speech.append(item)
        self._wake()
        self._prefetch_head()

    def _wake(self) -> None:
        if self._speech_task is None or self._speech_task.done():
            self._speech_task = asyncio.create_task(self._speak_loop())
        self._speech_wake.set()

    def _playable(self, item: _Say) -> bool:
        if item.turn is not None and item.turn.muted:
            return False
        if item.kind == "filler":
            t = item.turn
            return t is not None and not (t.has_speech or t.ended) and not self._confirms
        if item.kind == "question":
            return bool(self._confirms) and self._confirms[0] is item.confirm
        return True

    def _next_item(self) -> _Say | None:
        if self._paused or self._user_talking:
            return None
        while self._speech:
            item = self._speech.popleft()
            if self._playable(item):
                return item
        return None

    def _prefetch_head(self) -> None:
        if self._prefetch is None or self._saying is None:
            return
        head = next((i for i in self._speech if self._playable(i)), None)
        if head is not None and not head.prefetched:
            head.prefetched = True
            try:
                self._prefetch(head.text)
            except Exception:
                log.exception("prefetch failed")

    async def _speak_loop(self) -> None:
        while True:
            item = self._next_item()
            if item is None:
                if self._saying is not None:
                    self._saying = None
                    self._refresh()
                self._speech_wake.clear()
                await self._speech_wake.wait()
                continue
            self._saying = item
            if item.kind == "reply" and item.turn is not None:
                item.turn.spoke = True
            if item.confirm is not None:
                item.confirm.queued = False
                item.confirm.asked = True
            self._refresh()
            self._prefetch_head()
            self._notify(self.on_say, item.text, item.kind)
            try:
                await self._speak(item.text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Not the text, and no traceback at INFO: an httpx error carries the URL (?text=…).
                log.warning("speak failed (%d chars): %s", len(item.text), type(e).__name__)
                log.debug("speak failure", exc_info=True)
            if item.confirm is not None:
                self._question_done(item.confirm)

    def _stop_voice(self) -> None:
        try:
            self._stop()
        except Exception:
            log.exception("stop_speaking failed")

    # ---- state ----

    def _derive(self) -> State:
        if self._paused:
            return "paused"
        if self._limited:
            return "limit"
        if self._offline:
            return "offline"
        if self._saying is not None:
            return "speaking"
        if self._confirms and self._confirms[0].asked:
            return "listening"
        if self._inputs or self._outstanding or (self._turn is not None and self._turn.solicited):
            return "thinking"
        if any(i.turn is not None and i.turn.solicited and self._playable(i) for i in self._speech):
            return "thinking"  # the reply is in, waiting for the voice
        return "listening"

    def _refresh(self) -> None:
        state = self._derive()
        if state != self.state:
            self.state = state
            try:
                self.on_state_change(state)
            except Exception:
                log.exception("on_state_change failed")

    @staticmethod
    def _notify(hook: Callable[..., None], *args: Any) -> None:
        try:
            hook(*args)
        except Exception:
            log.exception("watch hook failed")

    # ---- tasks ----

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()


class SynthSpeaker:
    """``speak`` / ``stop`` / ``prefetch`` built from a synthesizer and a player.

    ``prefetch(next)`` starts synthesizing the next sentence while the current one plays, so
    sentences follow each other without a gap. The daemon wires it as
    ``SynthSpeaker(voicevox.synth, lambda pcm: player.play(pcm, RATE), player.stop)``.
    """

    _MAX_CACHED = 3

    def __init__(
        self,
        synth: Callable[[str], Awaitable[np.ndarray]],
        play: Callable[[np.ndarray], Awaitable[bool]],
        stop_playback: Callable[[], None],
    ) -> None:
        self._synth = synth
        self._play = play
        self._stop_playback = stop_playback
        self._cache: dict[str, asyncio.Future] = {}
        self._generation = 0
        self._stop_signal: asyncio.Future | None = None

    def _start(self, text: str) -> asyncio.Future:
        fut = asyncio.ensure_future(self._synth(text))
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # no "never retrieved"
        return fut

    def prefetch(self, text: str) -> None:
        if text in self._cache:
            return
        self._cache[text] = self._start(text)
        while len(self._cache) > self._MAX_CACHED:
            self._cache.pop(next(iter(self._cache))).cancel()

    async def speak(self, text: str) -> bool:
        """True if played to the end, False if stop() cut it off (even mid-synthesis)."""
        synth = self._cache.pop(text, None) or self._start(text)
        generation = self._generation
        self._stop_signal = asyncio.get_running_loop().create_future()
        await asyncio.wait({synth, self._stop_signal}, return_when=asyncio.FIRST_COMPLETED)
        if generation != self._generation:
            synth.cancel()
            return False
        return await self._play(synth.result())

    def stop(self) -> None:
        self._generation += 1
        if self._stop_signal is not None and not self._stop_signal.done():
            self._stop_signal.set_result(None)
        for fut in self._cache.values():
            fut.cancel()
        self._cache.clear()
        self._stop_playback()
