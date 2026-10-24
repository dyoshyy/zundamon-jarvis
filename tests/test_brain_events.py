import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitInfo,
    ToolPermissionContext,
)

from zundamon_jarvis.brain import (
    ClaudeBrain,
    PermissionRequest,
    TaskDone,
    TextDelta,
    ToolUse,
    TurnEnd,
    UsageLimit,
    task_transition,
    to_events,
    AuthProblem,
)

RESET = 1792818600
RESET_DT = datetime.fromtimestamp(RESET, tz=timezone.utc)


def text_delta(text, parent=None):
    return StreamEvent(
        uuid="u", session_id="s", parent_tool_use_id=parent,
        event={"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
    )


def result(**kw):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                session_id="s", stop_reason="end_turn", result="ok", terminal_reason="completed")
    base.update(kw)
    return ResultMessage(**base)


def rate_limit(status, resets_at=RESET, overage_status="rejected"):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status, resets_at=resets_at, rate_limit_type="five_hour",
                                      overage_status=overage_status,
                                      overage_disabled_reason="org_level_disabled_until"),
        uuid="u", session_id="s",
    )


def task_started(task_id="t1", backgrounded=True):
    data = {"type": "system", "subtype": "task_started", "task_id": task_id, "tool_use_id": "toolu_1",
            "description": "Count notes", "subagent_type": "Explore", "is_backgrounded": backgrounded,
            "task_type": "local_agent"}
    return TaskStartedMessage(subtype="task_started", data=data, task_id=task_id, description="Count notes",
                              uuid="u", session_id="s", tool_use_id="toolu_1", task_type="local_agent")


def task_updated(task_id="t1", status="completed"):
    patch = {"status": status, "end_time": 1792802135570}
    return TaskUpdatedMessage(subtype="task_updated", data={"task_id": task_id, "patch": patch},
                              task_id=task_id, patch=patch, status=status, session_id="s", uuid="u")


def task_notification(task_id="t1", status="completed"):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=task_id, status=status,
                                   output_file="/tmp/x", summary="done", uuid="u", session_id="s")


# ---- to_events (pure) ----

def test_text_delta_stream_event():
    assert to_events(text_delta("明日は")) == [TextDelta("明日は")]


def test_subagent_text_is_not_spoken():
    assert to_events(text_delta("サブエージェントの独り言", parent="toolu_1")) == []


@pytest.mark.parametrize("event", [
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "..."}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{"}},
    {"type": "message_start", "message": {"usage": {}}},
    {"type": "content_block_stop", "index": 1},
])
def test_other_stream_events_ignored(event):
    assert to_events(StreamEvent(uuid="u", session_id="s", event=event)) == []


def test_result_message_ends_turn():
    assert to_events(result()) == [TurnEnd()]


def test_usage_limit_result_legacy_shape_with_reset():
    msg = result(is_error=True, result=f"Claude AI usage limit reached|{RESET}", api_error_status=429)
    assert to_events(msg) == [UsageLimit(RESET_DT), TurnEnd()]


def test_usage_limit_result_new_wording_without_reset():
    msg = result(is_error=True, result="You've hit your limit · resets 3pm (Asia/Tokyo)")
    assert to_events(msg) == [UsageLimit(None), TurnEnd()]


def test_usage_limit_result_from_errors_list():
    msg = result(subtype="error_during_execution", is_error=True, result=None,
                 errors=["Claude AI usage limit reached"])
    assert to_events(msg) == [UsageLimit(None), TurnEnd()]


def test_other_error_result_is_just_turn_end():
    msg = result(is_error=True, result="API Error: 529 Overloaded", api_error_status=529)
    assert to_events(msg) == [TurnEnd()]


def test_routine_rate_limit_status_is_ignored():
    # observed every turn: allowed, overage rejected (overage disabled for the org)
    assert to_events(rate_limit("allowed")) == []
    assert to_events(rate_limit("allowed_warning")) == []


def test_rejected_rate_limit_is_usage_limit():
    assert to_events(rate_limit("rejected")) == [UsageLimit(RESET_DT)]
    assert to_events(rate_limit("rejected", resets_at=None)) == [UsageLimit(None)]


def test_assistant_rate_limit_error_is_usage_limit():
    msg = AssistantMessage(content=[TextBlock("Claude AI usage limit reached")], model="m", error="rate_limit")
    assert to_events(msg) == [UsageLimit(None)]


def test_plain_assistant_and_system_messages_ignored():
    assert to_events(AssistantMessage(content=[TextBlock("はい")], model="m")) == []
    assert to_events(SystemMessage(subtype="init", data={})) == []
    assert to_events(task_started()) == []


def test_tool_use_blocks_become_tool_use_events():
    msg = AssistantMessage(content=[
        TextBlock("見てみるね"),
        ToolUseBlock("toolu_a", "Read", {"file_path": "/x/a.md"}),
        ToolUseBlock("toolu_b", "Grep", {"pattern": "p"}),
    ], model="m")
    assert to_events(msg) == [ToolUse("Read", {"file_path": "/x/a.md"}, None),
                              ToolUse("Grep", {"pattern": "p"}, None)]


def test_subagent_tool_use_carries_its_parent():
    msg = AssistantMessage(content=[ToolUseBlock("toolu_c", "Glob", {"pattern": "*"})], model="m",
                           parent_tool_use_id="toolu_agent")
    assert to_events(msg) == [ToolUse("Glob", {"pattern": "*"}, "toolu_agent")]


def test_tool_use_input_is_a_copy():
    inp = {"file_path": "/x"}
    (ev,) = to_events(AssistantMessage(content=[ToolUseBlock("t", "Read", inp)], model="m"))
    inp["file_path"] = "/changed"
    assert ev.input == {"file_path": "/x"}


def test_background_task_notification_is_task_done():
    assert to_events(task_notification("a", "completed")) == [TaskDone("a", "completed", "done")]
    assert to_events(task_updated("a", "completed")) == []  # the notification follows; once only


async def test_tool_use_events_reach_the_queue(brain):
    await brain.submit("メモ見て")
    FakeClient.instances[0].inbox.put_nowait(
        AssistantMessage(content=[ToolUseBlock("t1", "Read", {"file_path": "/x/m.md"})], model="m"))
    assert await nxt(brain.events()) == ToolUse("Read", {"file_path": "/x/m.md"}, None)
    assert brain.busy  # a tool use is still part of the turn
    await brain.close()


# ---- task_transition (pure) ----

def test_task_transitions():
    assert task_transition(task_started("a")) == ("start", "a")
    assert task_transition(task_started("f", backgrounded=False)) is None
    progress = TaskProgressMessage(subtype="task_progress", data={}, task_id="a", description="d",
                                   usage={"total_tokens": 1, "tool_uses": 1, "duration_ms": 1},
                                   uuid="u", session_id="s")
    assert task_transition(progress) is None
    assert task_transition(task_updated("a", "running")) is None
    assert task_transition(task_updated("a", "completed")) == ("end", "a")
    assert task_transition(task_updated("a", "killed")) == ("end", "a")
    assert task_transition(task_notification("a", "failed")) == ("end", "a")
    assert task_transition(result()) is None


# ---- ClaudeBrain wiring with a fake SDK client ----

class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.queries: list[str] = []
        self.connected = False
        self.interrupted = False
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def query(self, text):
        self.queries.append(text)

    async def receive_messages(self):
        while True:
            msg = await self.inbox.get()
            if msg is None:
                return
            yield msg

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.connected = False
        self.inbox.put_nowait(None)


@pytest.fixture
def brain():
    FakeClient.instances.clear()
    b = ClaudeBrain(Path("/work/dir"), client_factory=FakeClient)
    yield b


async def nxt(it, timeout=1.0):
    return await asyncio.wait_for(anext(it), timeout)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def ctx(agent_id=None):
    return ToolPermissionContext(signal=None, suggestions=[], tool_use_id="toolu_x", agent_id=agent_id)


async def test_connects_on_first_submit_with_expected_options(brain, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    assert FakeClient.instances == []
    await brain.submit("明日の予定は？")
    c = FakeClient.instances[0]
    assert c.connected and c.queries == ["明日の予定は？"]
    o = c.options
    assert o.cwd == "/work/dir"
    assert o.add_dirs == [] and "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD" not in o.env
    assert o.setting_sources == ["user", "project", "local"]
    assert o.include_partial_messages is True
    assert o.system_prompt["type"] == "preset" and o.system_prompt["preset"] == "claude_code"
    assert "[[silent]]" in o.system_prompt["append"]
    assert o.cli_path and o.cli_path.endswith("claude")
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in o.env
    await brain.submit("もう一つ")
    assert len(FakeClient.instances) == 1 and c.queries[-1] == "もう一つ"
    await brain.close()
    assert not c.connected


async def test_turn_streams_main_text_only_and_tracks_busy(brain):
    it = brain.events()
    assert not brain.busy
    await brain.submit("やあ")
    assert brain.busy
    c = FakeClient.instances[0]
    c.inbox.put_nowait(text_delta("サブの独り言", parent="toolu_1"))
    c.inbox.put_nowait(text_delta("こんにちは。"))
    assert await nxt(it) == TextDelta("こんにちは。")
    c.inbox.put_nowait(result())
    assert await nxt(it) == TurnEnd()
    assert not brain.busy
    await brain.close()


async def test_unsolicited_main_turn_sets_busy_and_ends(brain):
    it = brain.events()
    await brain.submit("裏で数えて")
    c = FakeClient.instances[0]
    c.inbox.put_nowait(task_started("bg"))
    c.inbox.put_nowait(text_delta("裏でやっとくね。"))
    assert await nxt(it) == TextDelta("裏でやっとくね。")
    c.inbox.put_nowait(result())
    assert await nxt(it) == TurnEnd()
    assert not brain.busy and brain.background_running
    # background finishes, then the main agent starts a new turn by itself
    c.inbox.put_nowait(text_delta("サブの途中経過", parent="toolu_1"))
    c.inbox.put_nowait(task_updated("bg", "completed"))
    c.inbox.put_nowait(text_delta("数え終わったよ。"))
    assert await nxt(it) == TextDelta("数え終わったよ。")
    assert brain.busy and not brain.background_running
    c.inbox.put_nowait(result())
    assert await nxt(it) == TurnEnd()
    assert not brain.busy
    await brain.close()


async def test_read_tools_are_allowed_without_asking(brain):
    it = brain.events()
    await brain.submit("x")
    cb = FakeClient.instances[0].options.can_use_tool
    r = await asyncio.wait_for(cb("mcp__claude_ai_Google_Calendar__list_events", {}, ctx()), 1)
    assert isinstance(r, PermissionResultAllow)
    r = await asyncio.wait_for(cb("Bash", {"command": "git log --oneline -5"}, ctx()), 1)
    assert isinstance(r, PermissionResultAllow)
    with pytest.raises(asyncio.TimeoutError):
        await nxt(it, timeout=0.05)
    await brain.close()


@pytest.mark.parametrize("agent_id,ok", [(None, True), (None, False), ("bg-agent", True), ("bg-agent", False)])
async def test_confirm_required_tool_waits_for_decision(brain, agent_id, ok):
    it = brain.events()
    await brain.submit("x")
    cb = FakeClient.instances[0].options.can_use_tool
    inp = {"summary": "ジム", "start": {"dateTime": "2026-10-25T08:30:00+09:00"}}
    pending = asyncio.create_task(cb("mcp__claude_ai_Google_Calendar__create_event", inp, ctx(agent_id)))
    ev = await nxt(it)
    assert isinstance(ev, PermissionRequest)
    assert ev.tool == "mcp__claude_ai_Google_Calendar__create_event" and ev.input == inp
    await settle()
    assert not pending.done()  # never runs until the user answers
    ev.decide(ok)
    r = await asyncio.wait_for(pending, 1)
    assert isinstance(r, PermissionResultAllow if ok else PermissionResultDeny)
    ev.decide(True)  # a second call is harmless
    await brain.close()


@pytest.mark.parametrize("tool,inp", [
    ("Write", {"file_path": "/home/alice/notes/a\x00b.md"}),
    ("Write", None),
])
async def test_odd_input_asks_instead_of_running(brain, tool, inp, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("odd input")

    monkeypatch.setattr("zundamon_jarvis.brain.needs_confirmation", boom)  # the check itself raises
    it = brain.events()
    await brain.submit("x")
    cb = FakeClient.instances[0].options.can_use_tool
    pending = asyncio.create_task(cb(tool, inp, ctx()))
    ev = await nxt(it)
    assert isinstance(ev, PermissionRequest)
    await settle()
    assert not pending.done()
    ev.decide(False)
    assert isinstance(await asyncio.wait_for(pending, 1), PermissionResultDeny)
    await brain.close()


async def test_close_denies_pending_permission(brain):
    it = brain.events()
    await brain.submit("x")
    cb = FakeClient.instances[0].options.can_use_tool
    pending = asyncio.create_task(cb("mcp__claude_ai_Gmail__send_message", {}, ctx()))
    assert isinstance(await nxt(it), PermissionRequest)
    await brain.close()
    assert isinstance(await asyncio.wait_for(pending, 1), PermissionResultDeny)


async def test_interrupt_denies_main_turn_question_but_keeps_background_one(brain):
    it = brain.events()
    await brain.submit("x")
    c = FakeClient.instances[0]
    main = asyncio.create_task(c.options.can_use_tool("mcp__claude_ai_Gmail__send_message", {}, ctx()))
    bg = asyncio.create_task(c.options.can_use_tool("mcp__claude_ai_Gmail__send_message", {}, ctx("bg")))
    assert isinstance(await nxt(it), PermissionRequest)
    bg_req = await nxt(it)
    await brain.interrupt()
    assert c.interrupted
    assert isinstance(await asyncio.wait_for(main, 1), PermissionResultDeny)
    await settle()
    assert not bg.done()
    bg_req.decide(True)
    assert isinstance(await asyncio.wait_for(bg, 1), PermissionResultAllow)
    await brain.close()


async def test_interrupt_before_connect_is_noop(brain):
    await brain.interrupt()
    await brain.close()


async def test_usage_limit_reported_once_per_turn(brain):
    it = brain.events()
    await brain.submit("x")
    c = FakeClient.instances[0]
    c.inbox.put_nowait(rate_limit("rejected"))
    c.inbox.put_nowait(result(is_error=True, result=f"Claude AI usage limit reached|{RESET}"))
    assert await nxt(it) == UsageLimit(RESET_DT)
    assert await nxt(it) == TurnEnd()
    await brain.close()


async def test_reader_crash_ends_turn_and_reconnects_next_time(brain):
    it = brain.events()
    await brain.submit("x")
    c = FakeClient.instances[0]
    c.inbox.put_nowait(None)  # stream ends unexpectedly (CLI died)
    ev = await nxt(it)
    assert ev == TurnEnd() and ev.ok is False
    assert not brain.busy
    await brain.submit("again")
    assert len(FakeClient.instances) == 2
    await brain.close()


def test_voice_mode_prompt_exists():
    from zundamon_jarvis import brain

    # package data: resolves inside the installed package, not the checkout root
    assert brain.VOICE_PROMPT.parent.parent == Path(brain.__file__).parent
    text = brain.VOICE_PROMPT.read_text()
    assert "[[silent]]" in text and "run_in_background" in text


def test_options_carry_the_model():
    assert ClaudeBrain(Path("/work/dir"), client_factory=FakeClient, model="sonnet")._options().model == "sonnet"


@pytest.mark.parametrize("model", [None, ""])
def test_options_without_model_inherit_cli_default(model):
    assert ClaudeBrain(Path("/work/dir"), client_factory=FakeClient, model=model)._options().model is None


def test_brain_options_carry_cwd_and_add_dirs():
    b = ClaudeBrain(Path("/w"), add_dirs=[Path("/kb")], client_factory=FakeClient)
    o = b._options()
    assert o.cwd == "/w" and o.add_dirs == ["/kb"]
    assert o.env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"


# ---- authentication problems ----

@pytest.mark.parametrize("text", [
    "Invalid API key · Please run /login",
    "API Error: 401 Unauthorized",
    "OAuth token has expired",
    "Failed to authenticate",
    "Not logged in",
])
def test_auth_error_result_reports_auth_problem(text):
    assert to_events(result(is_error=True, result=text)) == [AuthProblem(), TurnEnd()]


def test_auth_problem_from_errors_list():
    msg = result(subtype="error_during_execution", is_error=True, result=None, errors=["401 unauthorized"])
    assert to_events(msg) == [AuthProblem(), TurnEnd()]


def test_auth_words_in_a_successful_result_are_not_a_problem():
    assert to_events(result(result="the login page returns 401")) == [TurnEnd()]


def test_api_error_status_401_403_is_an_auth_problem():
    for code in (401, 403):
        msg = result(is_error=True, result="API Error", api_error_status=code)
        assert to_events(msg) == [AuthProblem(), TurnEnd()]


def test_assistant_authentication_failed_error():
    msg = AssistantMessage(content=[TextBlock("Invalid API key")], model="m", error="authentication_failed")
    assert to_events(msg) == [AuthProblem()]


@pytest.mark.parametrize("text", [
    "MCP tool slack_search failed: server needs authentication",
    "Notion connector: please authenticate in settings (oauth required)",
    "request id req_4010 used 401 tokens",
    "login page fetched",
])
def test_mcp_and_incidental_text_is_not_an_auth_problem(text):
    msg = result(subtype="error_during_execution", is_error=True, result=None, errors=[text])
    assert to_events(msg) == [TurnEnd()]


def test_error_result_turn_end_is_not_ok():
    ev = to_events(result(is_error=True, result="API Error: 529"))[0]
    assert ev.ok is False and ev == TurnEnd()


def test_auth_text_matches_exceptions():
    from zundamon_jarvis.brain import is_auth_error
    assert is_auth_error(Exception("Invalid API key"))
    assert not is_auth_error(Exception("connection reset"))


def test_failed_turn_log_has_no_result_text(caplog):
    import logging

    brain = ClaudeBrain(Path("/w"), client_factory=FakeClient)
    with caplog.at_level(logging.WARNING, logger="zundamon_jarvis.brain"):
        brain._handle(result(subtype="error_during_execution", is_error=True, result="秘密の返事です"))
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "Claude turn failed" in msgs and "error_during_execution" in msgs
    assert "秘密" not in msgs
