"""Claude as the daemon's brain.

``ClaudeBrain`` drives the installed ``claude`` CLI through claude-agent-sdk, running in the
configured working directory (so its CLAUDE.md, memory and hooks apply), and turns the SDK stream into a
few simple events for the orchestrator. Tool calls that ``policy.needs_confirmation`` flags are
held until the user answers out loud; everything else is allowed straight away (the SDK sends
read tools to the callback too).

One persistent ``receive_messages()`` reader is used, not ``receive_response()``: background
Agent tasks keep streaming after a turn's ResultMessage and later start a new main-agent turn
(with its own ResultMessage) without any ``query()``.
"""

import asyncio
import logging
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from zundamon_jarvis.policy import describe_action, needs_confirmation

log = logging.getLogger(__name__)

# Package data, so it is there in a `uv tool install` too (not only in a checkout).
VOICE_PROMPT = Path(str(resources.files("zundamon_jarvis") / "prompts" / "voice-mode.md"))
DENY_MESSAGE = "The user declined at the spoken confirmation, so this was not run."


# ---- events ----

@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class TurnEnd:
    ok: bool = field(default=True, compare=False)  # False: the turn ended in an error result


@dataclass(frozen=True)
class UsageLimit:
    reset_at: datetime | None = None


@dataclass(frozen=True)
class AuthProblem:
    """Claude rejected the turn for lack of a valid login. Never spoken; shown as ``login: false``."""


@dataclass
class PermissionRequest:
    tool: str
    input: dict
    decide: Callable[[bool], None] = field(compare=False, repr=False)


@dataclass(frozen=True)
class ToolUse:
    """A tool call (main agent, or a subagent: ``agent_id`` is its Agent tool_use id).

    Only shown in the watch window; never spoken.
    """

    name: str
    input: dict = field(default_factory=dict, hash=False)
    agent_id: str | None = None


@dataclass(frozen=True)
class TaskDone:
    """A background task finished (completed / failed / stopped). Only shown, never spoken."""

    task_id: str
    status: str
    summary: str = ""


BrainEvent = TextDelta | TurnEnd | UsageLimit | AuthProblem | PermissionRequest | ToolUse | TaskDone


class Brain(Protocol):
    busy: bool
    background_running: bool

    async def submit(self, text: str) -> None: ...
    def events(self) -> AsyncIterator[BrainEvent]: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


# ---- pure translation of SDK messages ----

# The rejected / limit-hit shape was not observed in the spike, so match loosely:
# the legacy CLI text is "Claude AI usage limit reached|<unix reset>", newer ones say
# "You've hit your limit · resets 3pm".
_LIMIT_TEXT = re.compile(r"usage limit|hit your limit|limit reached|rate limit", re.IGNORECASE)
# Fallback only (the primary signals are api_error_status 401/403 and error="authentication_failed"):
# specific phrases, so an MCP tool saying "needs authentication" or a stray number does not match.
_AUTH_TEXT = re.compile(
    r"not logged in|please run /login|invalid api key|failed to authenticate|authentication_error"
    r"|oauth token (has )?(expired|revoked)|\b401\b[^.\n]{0,40}unauthorized|unauthorized[^.\n]{0,40}\b401\b",
    re.IGNORECASE)
_RESET_TS = re.compile(r"\|(\d{9,})")


def _from_unix(ts: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _reset_in(text: str) -> datetime | None:
    m = _RESET_TS.search(text)
    return _from_unix(m.group(1)) if m else None


def _result_usage_limit(msg: ResultMessage) -> UsageLimit | None:
    failed = msg.is_error or msg.subtype != "success" or msg.api_error_status == 429
    if not failed:
        return None
    text = " ".join([msg.result or "", *(msg.errors or [])])
    if msg.api_error_status == 429 or _LIMIT_TEXT.search(text):
        return UsageLimit(_reset_in(text))
    return None


def is_auth_error(exc: BaseException | str) -> bool:
    return bool(_AUTH_TEXT.search(str(exc)))


def _result_auth_problem(msg: ResultMessage) -> bool:
    if not (msg.is_error or msg.subtype != "success"):
        return False
    if msg.api_error_status in (401, 403):
        return True
    return is_auth_error(" ".join([msg.result or "", *(msg.errors or [])]))


def to_events(msg: Any) -> list[BrainEvent]:
    """Translate one SDK message into BrainEvents. Only main-agent text is spoken; tool uses
    (main and subagent) and background-task ends are reported for the watch window."""
    if isinstance(msg, TaskNotificationMessage):
        return [TaskDone(msg.task_id, msg.status, msg.summary or "")]
    if isinstance(msg, StreamEvent):
        if msg.parent_tool_use_id is not None:
            return []  # subagent narration
        ev = msg.event or {}
        delta = ev.get("delta") or {}
        if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta" and delta.get("text"):
            return [TextDelta(delta["text"])]
        return []
    if isinstance(msg, ResultMessage):
        limit = _result_usage_limit(msg)
        ok = not (msg.is_error or msg.subtype != "success")
        if limit:
            return [limit, TurnEnd(ok)]
        return [AuthProblem(), TurnEnd(ok)] if _result_auth_problem(msg) else [TurnEnd(ok)]
    if isinstance(msg, RateLimitEvent):
        info = msg.rate_limit_info
        # Routine status updates arrive every turn ("allowed", overage "rejected"); only the
        # main window being rejected means the limit is hit.
        return [UsageLimit(_from_unix(info.resets_at))] if info.status == "rejected" else []
    if isinstance(msg, AssistantMessage):
        if msg.error == "authentication_failed":
            return [AuthProblem()]
        if msg.error == "rate_limit":
            text = " ".join(b.text for b in msg.content if isinstance(b, TextBlock))
            return [UsageLimit(_reset_in(text))]
        return [ToolUse(b.name, dict(b.input) if isinstance(b.input, dict) else {},
                        msg.parent_tool_use_id)
                for b in msg.content if isinstance(b, ToolUseBlock)]
    return []


def task_transition(msg: Any) -> tuple[Literal["start", "end"], str] | None:
    """Background-task bookkeeping: ("start", id) / ("end", id) / None."""
    if isinstance(msg, TaskStartedMessage):
        return ("start", msg.task_id) if msg.data.get("is_backgrounded", True) else None
    if isinstance(msg, TaskUpdatedMessage):
        status = msg.status or (msg.patch or {}).get("status")
        return ("end", msg.task_id) if status in TERMINAL_TASK_STATUSES else None
    if isinstance(msg, TaskNotificationMessage):
        return ("end", msg.task_id) if msg.status in TERMINAL_TASK_STATUSES else None
    return None


def _is_main_agent_output(msg: Any) -> bool:
    return isinstance(msg, (StreamEvent, AssistantMessage)) and msg.parent_tool_use_id is None


# ---- the SDK wiring ----

class ClaudeBrain:
    def __init__(
        self,
        cwd: Path,
        add_dirs: list[Path] | None = None,
        *,
        client_factory: Callable[[ClaudeAgentOptions], Any] = ClaudeSDKClient,
        prompt_path: Path = VOICE_PROMPT,
        model: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.add_dirs = list(add_dirs or [])
        self.model = model  # None: inherit the CLI's default (~/.claude/settings.json)
        self.busy = False
        self._factory = client_factory
        self._prompt_path = prompt_path
        self._queue: asyncio.Queue[BrainEvent] = asyncio.Queue()
        self._client: Any = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[asyncio.Future, str | None] = {}  # future -> agent_id
        self._active_tasks: set[str] = set()
        self._limit_reported = False
        self._connect_lock = asyncio.Lock()

    @property
    def background_running(self) -> bool:
        return bool(self._active_tasks)

    def _options(self) -> ClaudeAgentOptions:
        if os.environ.pop("ANTHROPIC_API_KEY", None) is not None:
            # The SDK passes os.environ through to the CLI; the key would switch it off the
            # subscription onto API billing.
            log.warning("ANTHROPIC_API_KEY removed from the environment; using the claude login")
        extra: dict[str, Any] = {"model": self.model} if self.model else {}
        if self.add_dirs:
            # The CLI reads CLAUDE.md from --add-dir directories only with this switch.
            extra["env"] = {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}
        return ClaudeAgentOptions(
            **extra,
            cli_path=shutil.which("claude"),
            cwd=str(self.cwd),
            add_dirs=[str(d) for d in self.add_dirs],
            setting_sources=["user", "project", "local"],
            include_partial_messages=True,
            can_use_tool=self._can_use_tool,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": self._prompt_path.read_text(encoding="utf-8")},
        )

    async def _ensure_connected(self) -> Any:
        async with self._connect_lock:
            if self._client is None:
                client = self._factory(self._options())
                await client.connect()
                self._client = client
                self._reader = asyncio.create_task(self._read(client))
            return self._client

    async def submit(self, text: str) -> None:
        client = await self._ensure_connected()
        self.busy = True
        self._limit_reported = False
        try:
            await client.query(text)
        except BaseException:
            self.busy = False
            raise

    async def events(self) -> AsyncIterator[BrainEvent]:
        while True:
            yield await self._queue.get()

    async def interrupt(self) -> None:
        if self._client is None:
            return
        # Questions from the main turn are moot once it is stopped; background agents keep theirs.
        self._deny_pending(main_only=True)
        await self._client.interrupt()

    async def close(self) -> None:
        client, self._client = self._client, None
        self._deny_pending()
        if client is not None:
            try:
                await client.disconnect()  # fires the SessionEnd hook (conversation capture)
            except Exception:
                log.exception("disconnect failed")
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        self.busy = False
        self._active_tasks.clear()

    # -- internals --

    async def _read(self, client: Any) -> None:
        try:
            async for msg in client.receive_messages():
                self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Claude stream failed")
            if is_auth_error(e):
                self._queue.put_nowait(AuthProblem())
        finally:
            if self._client is client:  # ended on its own: the CLI went away
                log.warning("Claude stream ended; will reconnect on the next submit")
                self._client = None
                self._active_tasks.clear()
                self._deny_pending()
                if self.busy:
                    self.busy = False
                    self._queue.put_nowait(TurnEnd(ok=False))

    def _handle(self, msg: Any) -> None:
        tr = task_transition(msg)
        if tr is not None:
            kind, task_id = tr
            if kind == "start":
                self._active_tasks.add(task_id)
            else:
                self._active_tasks.discard(task_id)
        if _is_main_agent_output(msg):
            self.busy = True  # also covers the unsolicited turn after a background task
        if isinstance(msg, ResultMessage) and (msg.is_error or msg.subtype != "success"):
            # Never the result text: it can be (part of) a reply, and the log ends up in doctor output.
            log.warning("Claude turn failed (subtype=%s, api_error_status=%s, result %d chars)",
                        msg.subtype, msg.api_error_status, len(msg.result or ""))
        for ev in to_events(msg):
            if isinstance(ev, UsageLimit):
                if self._limit_reported:
                    continue
                self._limit_reported = True
            elif isinstance(ev, TurnEnd):
                self.busy = False
            self._queue.put_nowait(ev)

    def _deny_pending(self, main_only: bool = False) -> None:
        for fut, agent_id in list(self._pending.items()):
            if main_only and agent_id is not None:
                continue
            if not fut.done():
                fut.set_result(False)

    async def _can_use_tool(
        self, tool: str, input: dict, context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        try:
            ask = needs_confirmation(tool, input, self.cwd)
        except Exception:  # odd input (e.g. a NUL in a path): ask rather than run
            log.exception("confirmation check failed for %s; asking", tool)
            ask = True
        if not ask:
            return PermissionResultAllow()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()

        def decide(ok: bool) -> None:
            def resolve() -> None:
                if not fut.done():
                    fut.set_result(bool(ok))
            try:
                loop.call_soon_threadsafe(resolve)
            except RuntimeError:  # loop already closed
                pass

        self._pending[fut] = context.agent_id if context is not None else None
        self._queue.put_nowait(PermissionRequest(tool, dict(input) if isinstance(input, dict) else {}, decide))
        try:
            ok = await fut
        finally:
            self._pending.pop(fut, None)
        return PermissionResultAllow() if ok else PermissionResultDeny(message=DENY_MESSAGE)


# ---- manual check: uv run python -m zundamon_jarvis.brain "What is on my calendar tomorrow?" ----

async def _main(question: str) -> None:
    from zundamon_jarvis.config import load_config

    cfg = load_config()
    brain = ClaudeBrain(cfg.workdir, [cfg.knowledge_dir] if cfg.knowledge_dir else [],
                        model=cfg.model)
    try:
        await brain.submit(question)
        async for ev in brain.events():
            if isinstance(ev, TextDelta):
                print(ev.text, end="", flush=True)
            elif isinstance(ev, PermissionRequest):
                print(f"\n[confirm] {describe_action(ev.tool, ev.input)} -> declined (manual check)", flush=True)
                ev.decide(False)
            elif isinstance(ev, UsageLimit):
                print(f"\n[limit] reset_at={ev.reset_at}", flush=True)
            elif isinstance(ev, ToolUse):
                print(f"\n[tool] {ev.name}{' (sub)' if ev.agent_id else ''}", flush=True)
            elif isinstance(ev, TurnEnd):
                print(flush=True)
                if not brain.busy:
                    break
    finally:
        await brain.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_main(" ".join(sys.argv[1:]) or "What is on my calendar tomorrow?"))
