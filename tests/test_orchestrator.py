import asyncio
from datetime import datetime, timezone

import numpy as np
from fakes import BrainFactory, FakeBrain, FakeClock, FakeSpeaker, settle

import zundamon_jarvis.orchestrator as orchestrator_module
from zundamon_jarvis.brain import AuthProblem, PermissionRequest, TaskDone, TextDelta, ToolUse, TurnEnd, UsageLimit
from zundamon_jarvis.orchestrator import (
    FILLERS,
    LIMIT_NOTICE,
    Orchestrator,
    OrchestratorConfig,
    SynthSpeaker,
)
from zundamon_jarvis.policy import describe_action

CAL_TOOL = "mcp__claude_ai_Google_Calendar__create_event"
CAL_INPUT = {"summary": "ジム", "start": {"dateTime": "2026-10-25T08:30:00+09:00"}}
QUESTION = describe_action(CAL_TOOL, CAL_INPUT)


class Rig:
    def __init__(self, *brains: FakeBrain, block: bool = False) -> None:
        self.factory = BrainFactory(*brains)
        self.speaker = FakeSpeaker(block=block)
        self.clock = FakeClock()
        self.heard: list[object] = []
        self.states: list[str] = []
        self.orch = Orchestrator(
            brain_factory=self.factory,
            transcribe=self._transcribe,
            speak=self.speaker.speak,
            stop_speaking=self.speaker.stop,
            clock=self.clock,
            config=OrchestratorConfig(),
            prefetch=self.speaker.prefetch,
        )
        self.orch.on_state_change = self.states.append

    def _transcribe(self, audio):
        # The "audio" in these tests is the transcript itself.
        self.heard.append(audio)
        return audio

    @property
    def brain(self) -> FakeBrain:
        return self.factory.made[-1]

    async def say(self, text: str) -> None:
        await self.orch.on_speech_started()
        await self.orch.on_utterance(text)
        await settle()


def calendar_request(decisions: list) -> PermissionRequest:
    return PermissionRequest(CAL_TOOL, dict(CAL_INPUT), decisions.append)


# ---- speaking replies ----

async def test_reply_is_spoken_sentence_by_sentence():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TextDelta("いいと思う。"), TurnEnd()]]))
    await rig.say("これどう思う？")
    assert rig.brain.submitted == ["これどう思う？"]
    assert rig.speaker.spoken == ["うん。", "いいと思う。"]
    assert rig.orch.state == "listening"


async def test_silent_reply_speaks_nothing():
    rig = Rig(FakeBrain([[TextDelta("[[sil"), TextDelta("ent]]"), TurnEnd()]]))
    await rig.say("よし")
    assert rig.brain.submitted == ["よし"]
    assert rig.speaker.spoken == []


async def test_hallucinated_transcript_is_not_sent():
    rig = Rig()
    await rig.say("ご視聴ありがとうございました")
    assert rig.heard == ["ご視聴ありがとうございました"]
    assert all(b.submitted == [] for b in rig.factory.made)


async def test_punctuation_only_fragments_and_markdown_are_not_spoken():
    rig = Rig(FakeBrain([[TextDelta("**大事**なのは睡眠！？"), TextDelta("」"), TurnEnd()]]))
    await rig.say("何が大事？")
    assert rig.speaker.spoken == ["大事なのは睡眠！"]


async def test_filler_after_3s_once():
    rig = Rig(FakeBrain([[]]))
    await rig.say("明日の予定は？")
    await rig.clock.advance(2.9)
    assert rig.speaker.spoken == []
    await rig.clock.advance(0.1)
    assert len(rig.speaker.spoken) == 1 and rig.speaker.spoken[0] in FILLERS
    await rig.clock.advance(30)
    assert len(rig.speaker.spoken) == 1
    rig.brain.emit(TextDelta("一時から会議だよ。"), TurnEnd())
    await settle()
    assert rig.speaker.spoken[1:] == ["一時から会議だよ。"]


async def test_no_filler_when_first_sentence_comes_at_2_9s():
    rig = Rig(FakeBrain([[]]))
    await rig.say("明日の予定は？")
    await rig.clock.advance(2.9)
    rig.brain.emit(TextDelta("一時から会議だよ。"))
    await settle()
    await rig.clock.advance(10)
    rig.brain.emit(TurnEnd())
    await settle()
    assert rig.speaker.spoken == ["一時から会議だよ。"]


async def test_no_filler_while_a_silent_reply_is_streaming():
    rig = Rig(FakeBrain([[TextDelta("[[sil")]]))
    await rig.say("えーっと")
    await rig.clock.advance(5)
    rig.brain.emit(TextDelta("ent]]"), TurnEnd())
    await settle()
    assert rig.speaker.spoken == []


async def test_next_sentence_is_prefetched_while_speaking():
    rig = Rig(FakeBrain([[TextDelta("一つ目。"), TextDelta("二つ目。"), TurnEnd()]]), block=True)
    await rig.say("教えて")
    assert rig.speaker.spoken == ["一つ目。"]
    assert rig.speaker.prefetched == ["二つ目。"]
    rig.speaker.finish()
    await settle()
    assert rig.speaker.spoken == ["一つ目。", "二つ目。"]


async def test_state_changes_are_reported():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TurnEnd()]]), block=True)
    await rig.say("どう？")
    assert rig.orch.state == "speaking"
    rig.speaker.finish()
    await settle()
    assert rig.orch.state == "listening"
    assert rig.states == ["thinking", "speaking", "listening"]


# ---- barge-in and queueing ----

async def test_barge_in_stops_speech_and_interrupts():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TextDelta("いいと思う。")]], interrupt_ends_turn=False),
              block=True)
    await rig.say("これどう？")
    assert rig.speaker.spoken == ["うん。"]
    await rig.orch.on_speech_started()
    await settle()
    assert rig.speaker.stops == 1
    assert rig.brain.interrupts == 1
    rig.brain.emit(TextDelta("それから。"), TurnEnd())  # the rest of the interrupted turn
    await rig.orch.on_utterance("待って")
    await settle()
    assert rig.speaker.spoken == ["うん。"]


async def test_barge_in_over_filler_mutes_the_reply_and_interrupts():
    rig = Rig(FakeBrain([[]], interrupt_ends_turn=False), block=True)
    await rig.say("明日の予定は？")
    await rig.clock.advance(3.0)
    assert len(rig.speaker.spoken) == 1 and rig.speaker.spoken[0] in FILLERS
    await rig.orch.on_speech_started()
    await settle()
    assert rig.speaker.stops == 1
    assert rig.brain.interrupts == 1
    rig.brain.emit(TextDelta("一時から会議だよ。"), TurnEnd())  # the stale answer
    await rig.orch.on_utterance("やっぱいい")
    await settle()
    assert len(rig.speaker.spoken) == 1
    assert rig.brain.submitted == ["明日の予定は？", "やっぱいい"]


async def test_barge_in_over_filler_with_confirmation_queued_only_stops_speech():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[]]), block=True)
    await rig.say("裏で予定入れといて")
    await rig.clock.advance(3.0)
    assert rig.speaker.spoken[0] in FILLERS
    rig.brain.emit(calendar_request(decisions))  # e.g. from a background agent
    await settle()
    await rig.orch.on_speech_started()
    await settle()
    assert rig.speaker.stops == 1
    assert rig.brain.interrupts == 0


async def test_barge_in_while_thinking_does_not_interrupt():
    rig = Rig(FakeBrain([[]]))
    await rig.say("一つ目")
    await rig.orch.on_speech_started()
    await settle()
    assert rig.brain.interrupts == 0


async def test_utterance_during_turn_is_queued():
    rig = Rig(FakeBrain([[], [TextDelta("了解。"), TurnEnd()]]))
    await rig.say("一つ目")
    await rig.say("二つ目")
    assert rig.brain.submitted == ["一つ目"]
    assert rig.brain.interrupts == 0
    rig.brain.emit(TextDelta("うん。"), TurnEnd())
    await settle()
    assert rig.brain.submitted == ["一つ目", "二つ目"]
    assert rig.speaker.spoken == ["うん。", "了解。"]


async def test_queue_holds_even_if_busy_reads_false_mid_turn():
    rig = Rig(FakeBrain([[]]))
    await rig.say("一つ目")
    rig.brain.busy = False  # submit landed during an unsolicited turn's ResultMessage
    await rig.say("二つ目")
    assert rig.brain.submitted == ["一つ目"]
    rig.brain.emit(TurnEnd())
    await settle()
    assert rig.brain.submitted == ["一つ目", "二つ目"]


async def test_failure_handling_turn_end_still_sends_queued_utterance(monkeypatch):
    rig = Rig(FakeBrain([[]]))
    await rig.say("一つ目")
    await rig.say("二つ目")

    def boom(self):
        raise RuntimeError("chunker broke")

    monkeypatch.setattr(orchestrator_module.SentenceChunker, "flush", boom)
    rig.brain.emit(TurnEnd())
    await settle()
    assert rig.brain.submitted == ["一つ目", "二つ目"]


async def test_speech_waits_while_user_is_talking():
    rig = Rig(FakeBrain([[TextDelta("いいよ。"), TurnEnd()], []]))
    await rig.say("裏で調べといて")
    assert rig.speaker.spoken == ["いいよ。"]
    await rig.orch.on_speech_started()
    rig.brain.emit(TextDelta("調べ終わったよ。"), TurnEnd())  # unsolicited report
    await settle()
    assert rig.speaker.spoken == ["いいよ。"]
    await rig.orch.on_utterance("ありがとう")
    await settle()
    assert rig.speaker.spoken == ["いいよ。", "調べ終わったよ。"]
    assert rig.brain.interrupts == 0


# ---- voice confirmation ----

async def test_confirmation_yes_allows():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    await rig.say("明日の朝8時半にジムの予定を入れて")
    assert rig.speaker.spoken == [QUESTION]
    await rig.say("うん")
    assert decisions == [True]
    assert rig.brain.submitted == ["明日の朝8時半にジムの予定を入れて"]


async def test_confirmation_no_denies():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    await rig.say("ジムの予定を入れて")
    await rig.say("やめて")
    assert decisions == [False]
    assert rig.brain.submitted == ["ジムの予定を入れて"]


async def test_unclear_confirmation_twice_denies_and_forwards():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)], []]))
    await rig.say("ジムの予定を入れて")
    await rig.say("天気どう？")
    assert decisions == []
    assert rig.speaker.spoken == [QUESTION, QUESTION]
    await rig.say("天気どう？")
    assert decisions == [False]
    rig.brain.emit(TextDelta("やめておくね。"), TurnEnd())
    await settle()
    assert rig.brain.submitted == ["ジムの予定を入れて", "天気どう？"]


async def test_confirmation_times_out_after_20s():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    await rig.say("ジムの予定を入れて")
    await rig.clock.advance(19.9)
    assert decisions == []
    await rig.clock.advance(0.1)
    assert decisions == [False]


async def test_confirmation_timeout_waits_for_an_answer_in_progress():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    await rig.say("ジムの予定を入れて")
    await rig.clock.advance(19)
    await rig.orch.on_speech_started()  # the answer starts at 19 s ...
    await rig.clock.advance(3)
    assert decisions == []
    await rig.orch.on_utterance("うん")  # ... and ends at 22 s
    await settle()
    assert decisions == [True]


async def test_confirmation_timeout_resumes_after_a_non_answer():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    await rig.say("ジムの予定を入れて")
    await rig.clock.advance(19)
    await rig.orch.on_speech_started()
    await rig.clock.advance(3)
    await rig.orch.on_utterance("ご視聴ありがとうございました")  # noise: not an answer
    await rig.clock.advance(0.9)
    assert decisions == []
    await rig.clock.advance(0.1)
    assert decisions == [False]


async def test_permission_requests_are_asked_one_at_a_time():
    first: list[bool] = []
    second: list[bool] = []
    other_input = {"summary": "歯医者", "start": {"dateTime": "2026-10-26T10:00:00+09:00"}}
    rig = Rig(FakeBrain([[calendar_request(first),
                          PermissionRequest(CAL_TOOL, other_input, second.append)]]))
    await rig.say("予定を二つ入れて")
    assert rig.speaker.spoken == [QUESTION]
    await rig.say("うん")
    assert first == [True] and second == []
    assert rig.speaker.spoken == [QUESTION, describe_action(CAL_TOOL, other_input)]
    await rig.say("だめ")
    assert second == [False]


async def test_barge_in_during_confirmation_only_stops_speech():
    # interrupt() would deny the main agent's pending request, so it must not be called.
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[TextDelta("入れるね。"), calendar_request(decisions)]]), block=True)
    await rig.say("ジムの予定を入れて")
    rig.speaker.finish()
    await settle()
    assert rig.speaker.spoken == ["入れるね。", QUESTION]
    await rig.orch.on_speech_started()
    await settle()
    assert rig.speaker.stops == 1
    assert rig.brain.interrupts == 0
    await rig.orch.on_utterance("うん")
    await settle()
    assert decisions == [True]
    rig.brain.emit(TextDelta("入れたよ。"), TurnEnd())
    await settle()
    rig.speaker.finish()
    await settle()
    assert rig.speaker.spoken == ["入れるね。", QUESTION, "入れたよ。"]


async def test_answer_after_question_keeps_the_reply():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[TextDelta("入れるね。"), calendar_request(decisions)]]))
    await rig.say("ジムの予定を入れて")
    assert rig.speaker.spoken == ["入れるね。", QUESTION]
    await rig.say("うん")  # speech starts between sentences of the turn: an answer, not a barge-in
    assert decisions == [True]
    assert rig.brain.interrupts == 0
    rig.brain.emit(TextDelta("入れたよ。"), TurnEnd())
    await settle()
    assert rig.speaker.spoken == ["入れるね。", QUESTION, "入れたよ。"]


async def test_barge_in_on_reply_with_pending_confirmation_does_not_interrupt():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[TextDelta("入れるね。"), calendar_request(decisions)]]), block=True)
    await rig.say("ジムの予定を入れて")
    assert rig.speaker.spoken == ["入れるね。"]  # the question waits behind it
    await rig.orch.on_speech_started()
    await settle()
    assert rig.speaker.stops == 1
    assert rig.brain.interrupts == 0
    assert decisions == []


# ---- pause ----

async def test_pause_stops_speech_and_holds_announcements():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TextDelta("それでね。")]]), block=True)
    await rig.say("裏でメール整理しといて")
    assert rig.speaker.spoken == ["うん。"]
    await rig.orch.pause()
    await settle()
    assert rig.speaker.stops == 1
    assert rig.orch.state == "paused"
    assert rig.brain.interrupts == 0  # the work goes on; only the voice stops
    rig.brain.emit(TurnEnd())
    rig.brain.emit(TextDelta("メールの整理、終わったよ。"), TurnEnd())
    await settle()
    assert rig.speaker.spoken == ["うん。"]
    await rig.orch.resume()
    await settle()
    assert rig.speaker.spoken == ["うん。", "メールの整理、終わったよ。"]
    rig.speaker.finish()
    await settle()
    assert rig.orch.state == "listening"


async def test_confirmation_is_held_during_pause():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[]]))
    await rig.say("裏で予定を入れといて")
    await rig.orch.pause()
    rig.brain.emit(calendar_request(decisions))
    await settle()
    await rig.clock.advance(60)
    assert decisions == [] and rig.speaker.spoken == []
    await rig.orch.resume()
    await settle()
    assert rig.speaker.spoken == [QUESTION]
    await rig.say("うん")
    assert decisions == [True]


# ---- usage limit ----

async def test_usage_limit_notice_once():
    reset_at = datetime.fromtimestamp(FakeClock().now() + 600, tz=timezone.utc)
    rig = Rig(FakeBrain([[UsageLimit(reset_at), TurnEnd()], []]))
    await rig.say("調べて")
    assert rig.speaker.spoken == [LIMIT_NOTICE]
    assert rig.orch.state == "limit"
    await rig.say("もう一回")
    rig.brain.emit(UsageLimit(reset_at))
    await settle()
    assert rig.brain.submitted == ["調べて"]
    assert rig.speaker.spoken == [LIMIT_NOTICE]
    await rig.clock.advance(599)
    assert rig.orch.state == "limit"
    await rig.clock.advance(1)
    assert rig.orch.state == "listening"
    await rig.say("戻った？")
    assert rig.brain.submitted == ["調べて", "戻った？"]


async def test_usage_limit_without_reset_retries_after_an_hour():
    rig = Rig(FakeBrain([[UsageLimit(None), TurnEnd()]]))
    await rig.say("調べて")
    assert rig.orch.state == "limit"
    await rig.clock.advance(3599)
    assert rig.orch.state == "limit"
    await rig.clock.advance(1)
    assert rig.orch.state == "listening"


# ---- idle session close ----

async def test_idle_30min_closes_session():
    first = FakeBrain([[TextDelta("うん。"), TurnEnd()]])
    second = FakeBrain()
    rig = Rig(first, second)
    await rig.say("おはよう")
    await rig.clock.advance(1799)
    assert not first.closed
    await rig.clock.advance(1)
    assert first.closed
    await rig.say("また来たよ")
    assert len(rig.factory.made) == 2
    assert second.submitted == ["また来たよ"]
    assert first.submitted == ["おはよう"]


async def test_idle_close_waits_for_background_work():
    brain = FakeBrain([[TextDelta("始めたよ。"), TurnEnd()]])
    rig = Rig(brain)
    await rig.say("裏で調べといて")
    brain.background_running = True
    await rig.clock.advance(1800)
    await rig.clock.advance(3600)
    assert not brain.closed
    brain.background_running = False
    await rig.clock.advance(60)
    assert brain.closed


async def test_utterance_during_idle_close_does_not_cut_the_close_short():
    first = FakeBrain([[TextDelta("うん。"), TurnEnd()]])
    first.close_gate = asyncio.Event()
    second = FakeBrain()
    rig = Rig(first, second)
    await rig.say("おはよう")
    await rig.clock.advance(1800)
    assert first.close_started and not first.closed
    await rig.say("また来たよ")  # resets the idle timer while close() is still running
    assert not first.close_cancelled
    assert second.submitted == []  # the new session waits for the old one to close
    first.close_gate.set()
    await settle()
    assert first.closed and not first.close_cancelled
    assert second.submitted == ["また来たよ"]
    assert first.submitted == ["おはよう"]


def _pending_tasks() -> list[asyncio.Task]:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me and not t.done()]


async def test_aclose_closes_the_brain_and_leaves_no_tasks():
    decisions: list[bool] = []
    rig = Rig(FakeBrain([[TextDelta("うん。"), TextDelta("それで。")], []]), block=True)
    await rig.say("どう？")
    await rig.say("あと")  # queued behind the turn; its idle and filler timers are pending
    rig.brain.emit(calendar_request(decisions))
    await settle()
    assert _pending_tasks()
    await rig.orch.aclose()
    await settle()
    assert rig.brain.closed
    assert rig.speaker.stops >= 1
    assert decisions == [False]
    assert _pending_tasks() == []


async def test_aclose_finishes_closing_even_if_cancelled():
    brain = FakeBrain([[TextDelta("うん。"), TurnEnd()]])
    brain.close_gate = asyncio.Event()
    rig = Rig(brain)
    await rig.say("おはよう")
    task = asyncio.create_task(rig.orch.aclose())
    await settle()
    assert brain.close_started
    task.cancel()
    await settle()
    brain.close_gate.set()
    await settle()
    assert brain.closed and not brain.close_cancelled
    assert _pending_tasks() == []


# ---- watch-window hooks: tool uses are shown, never spoken ----

class Watched(Rig):
    def __init__(self, *brains: FakeBrain, block: bool = False) -> None:
        super().__init__(*brains, block=block)
        self.brain_events: list[object] = []
        self.said: list[tuple[str, str]] = []
        self.confirms: list[tuple[str, bool, str]] = []
        self.orch.on_brain_event = self.brain_events.append
        self.orch.on_say = lambda text, kind: self.said.append((text, kind))
        self.orch.on_confirm = lambda q, ok, why: self.confirms.append((q, ok, why))


async def test_tool_use_is_not_spoken_and_changes_nothing():
    tool = ToolUse("Read", {"file_path": "/x/a.md"}, None)
    sub = ToolUse("Glob", {"pattern": "*"}, "toolu_agent")
    rig = Watched(FakeBrain([[tool, TextDelta("読んだよ。"), sub, TurnEnd()]]))
    await rig.say("メモ読んで")
    assert rig.speaker.spoken == ["読んだよ。"]
    assert rig.orch.state == "listening"
    assert rig.brain_events[0] == tool and sub in rig.brain_events


async def test_tool_use_only_turn_still_ends_and_sends_the_next_utterance():
    rig = Watched(FakeBrain([[ToolUse("Read", {}, None)], [TurnEnd()]]))
    await rig.say("一つ目")
    assert rig.orch.state == "thinking"  # the tool use did not end the turn
    await rig.say("二つ目")
    assert rig.brain.submitted == ["一つ目"]
    rig.brain.emit(TurnEnd())
    await settle()
    assert rig.brain.submitted == ["一つ目", "二つ目"]


async def test_background_task_done_is_not_spoken():
    rig = Watched(FakeBrain([[TurnEnd()]]))
    await rig.say("裏で数えて")
    rig.brain.emit(TaskDone("t1", "completed", "done"))
    await settle()
    assert rig.speaker.spoken == [] and rig.orch.state == "listening"
    assert TaskDone("t1", "completed", "done") in rig.brain_events


async def test_a_failing_brain_event_hook_does_not_break_the_turn():
    rig = Watched(FakeBrain([[ToolUse("Read", {}, None), TextDelta("はい。"), TurnEnd()]]))

    def boom(ev):
        raise RuntimeError("watch broke")

    rig.orch.on_brain_event = boom
    await rig.say("どう？")
    assert rig.speaker.spoken == ["はい。"]


async def test_on_say_reports_what_is_spoken_and_why():
    decisions: list[bool] = []
    rig = Watched(FakeBrain([[TextDelta("うん。"), TurnEnd()], [calendar_request(decisions)]]))
    await rig.say("どう？")
    await rig.say("ジムの予定を入れて")
    assert rig.said == [("うん。", "reply"), (QUESTION, "question")]


async def test_on_confirm_reports_yes_no_and_timeout():
    decisions: list[bool] = []
    rig = Watched(FakeBrain([[calendar_request(decisions)], [calendar_request(decisions)],
                             [calendar_request(decisions)]]))
    await rig.say("予定を入れて")
    await rig.say("うん")
    rig.brain.emit(TurnEnd())
    await rig.say("もう一つ入れて")
    await rig.say("やめて")
    rig.brain.emit(TurnEnd())
    await rig.say("三つ目も")
    await rig.clock.advance(20)
    assert rig.confirms == [(QUESTION, True, "answer"), (QUESTION, False, "answer"),
                            (QUESTION, False, "timeout")]


async def test_on_confirm_reports_unclear_twice():
    decisions: list[bool] = []
    rig = Watched(FakeBrain([[calendar_request(decisions)], []]))
    await rig.say("ジムの予定を入れて")
    await rig.say("天気どう？")
    await rig.say("晴れ？")
    assert rig.confirms == [(QUESTION, False, "unclear")]


# ---- SynthSpeaker: synth + playback with one-sentence lookahead ----

class FakePlayback:
    def __init__(self) -> None:
        self.played: list[float] = []
        self.stops = 0
        self._fut: asyncio.Future[bool] | None = None

    async def play(self, pcm: np.ndarray) -> bool:
        self.played.append(float(pcm[0]))
        self._fut = asyncio.get_running_loop().create_future()
        return await self._fut

    def finish(self) -> None:
        if self._fut is not None and not self._fut.done():
            self._fut.set_result(True)

    def stop(self) -> None:
        self.stops += 1
        if self._fut is not None and not self._fut.done():
            self._fut.set_result(False)


async def test_synth_speaker_synthesizes_next_sentence_during_playback():
    calls: list[str] = []

    async def synth(text: str) -> np.ndarray:
        calls.append(text)
        return np.array([float(len(calls))], dtype=np.float32)

    pb = FakePlayback()
    sp = SynthSpeaker(synth, pb.play, pb.stop)
    first = asyncio.create_task(sp.speak("一つ目。"))
    await settle()
    sp.prefetch("二つ目。")
    await settle()
    assert calls == ["一つ目。", "二つ目。"]  # synthesized while the first is still playing
    pb.finish()
    assert await first is True
    second = asyncio.create_task(sp.speak("二つ目。"))
    await settle()
    assert calls == ["一つ目。", "二つ目。"]  # reused, not synthesized again
    assert pb.played == [1.0, 2.0]
    pb.finish()
    assert await second is True


async def test_synth_speaker_stop_during_synthesis_returns_false():
    gate = asyncio.Event()

    async def synth(text: str) -> np.ndarray:
        await gate.wait()
        return np.zeros(1, dtype=np.float32)

    pb = FakePlayback()
    sp = SynthSpeaker(synth, pb.play, pb.stop)
    task = asyncio.create_task(sp.speak("長い文。"))
    await settle()
    sp.stop()
    assert await asyncio.wait_for(task, 1) is False
    gate.set()
    await settle()
    assert pb.played == []


# ---- settings window: read-only state and restarting the session ----

async def test_user_talking_and_saying_are_visible():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TurnEnd()]]), block=True)
    assert not rig.orch.user_talking and not rig.orch.saying and not rig.orch.has_session
    await rig.orch.on_speech_started()
    assert rig.orch.user_talking
    await rig.orch.on_utterance("やあ")
    await settle()
    assert not rig.orch.user_talking
    assert rig.orch.saying and rig.orch.has_session
    rig.speaker.finish()
    await settle()
    assert not rig.orch.saying


async def test_restart_session_closes_an_idle_session_and_the_next_utterance_starts_anew():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TurnEnd()]]),
              FakeBrain([[TextDelta("はい。"), TurnEnd()]]))
    assert rig.orch.restart_session() == "none"
    await rig.say("やあ")
    first = rig.brain
    assert rig.orch.restart_session() == "closed"
    await settle()
    assert first.closed and not rig.orch.has_session
    await rig.say("もう一回")
    assert rig.brain is not first and rig.brain.submitted == ["もう一回"]
    assert rig.speaker.spoken == ["うん。", "はい。"]


async def test_restart_session_refuses_while_anything_is_under_way():
    rig = Rig(FakeBrain([[TextDelta("うん。")]]))  # no TurnEnd: still busy
    await rig.say("やあ")
    assert rig.orch.restart_session() == "busy"
    assert not rig.brain.close_started
    rig.brain.emit(TurnEnd())
    await settle()
    rig.brain.background_running = True
    assert rig.orch.restart_session() == "busy"
    rig.brain.background_running = False
    assert rig.orch.restart_session() == "closed"


def test_language_detection_for_spoken_lines():
    from zundamon_jarvis.orchestrator import _is_japanese
    assert _is_japanese("明日の予定は？") and _is_japanese("ok これ")
    assert not _is_japanese("What is on my calendar?")


# ---- abandoned speech and diagnostics ----

async def test_abandoned_speech_clears_user_talking_and_releases_held_speech():
    rig = Rig(FakeBrain([[TextDelta("うん。"), TurnEnd()]]))
    await rig.say("やあ")
    rig.speaker.spoken.clear()
    await rig.orch.on_speech_started()  # the user starts talking ...
    assert rig.orch.user_talking
    rig.brain.emit(TextDelta("あとで。"), TurnEnd())  # an unsolicited line waits for them
    await settle()
    assert rig.speaker.spoken == []
    rig.orch.on_speech_abandoned()  # ... and their speech never became an Utterance
    await settle()
    assert not rig.orch.user_talking
    assert rig.speaker.spoken == ["あとで。"]


async def test_abandoned_speech_restarts_a_suspended_confirmation_clock():
    decisions = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    await rig.say("ジム入れて")
    assert rig.speaker.spoken == [QUESTION]
    await rig.orch.on_speech_started()
    await rig.clock.advance(60)  # suspended while they talk
    assert decisions == []
    rig.orch.on_speech_abandoned()
    await rig.clock.advance(20)
    assert decisions == [False]


async def test_abandoned_speech_without_talking_is_a_noop():
    rig = Rig()
    rig.orch.on_speech_abandoned()
    assert not rig.orch.user_talking and rig.orch.state == "listening"


async def test_debug_state():
    rig = Rig(FakeBrain([[TextDelta("うん。")]]), block=True)
    d = rig.orch.debug_state()
    assert d["user_talking"] is False and d["saying"] is None and d["turn"] is None
    assert d["has_session"] is False and d["queued_inputs"] == 0 and d["confirms"] == 0
    await rig.say("やあ")
    d = rig.orch.debug_state()
    assert d["saying"] == "reply" and d["has_session"] and d["brain_busy"]
    assert d["turn"]["solicited"] is True and d["turn"]["spoke"] is True
    import json
    json.dumps(d)  # it goes over the control socket


async def test_dropped_utterances_are_logged(caplog):
    import logging

    rig = Rig()
    with caplog.at_level(logging.INFO, logger="zundamon_jarvis.orchestrator"):
        await rig.say("")
        await rig.say("ご視聴ありがとうございました")
        await rig.orch.pause()
        await rig.orch.on_utterance("やあ")
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "dropped (empty)" in msgs and "dropped (hallucination)" in msgs
    assert "dropped (paused)" in msgs


# ---- login problem ----

async def test_auth_problem_is_silent_and_clears_on_the_next_good_turn():
    rig = Rig(FakeBrain([[AuthProblem(), TurnEnd()], [TextDelta("はい。"), TurnEnd()]]))
    await rig.say("調べて")
    assert rig.speaker.spoken == []
    assert rig.orch.login_ok is False
    await rig.say("もう一回")
    assert rig.orch.login_ok is True


async def test_auth_problem_clears_on_a_successful_tool_only_turn():
    rig = Rig(FakeBrain([[AuthProblem(), TurnEnd(False)], [ToolUse("Read", {}), TurnEnd()]]))
    await rig.say("調べて")
    assert rig.orch.login_ok is False
    await rig.say("もう一回")
    assert rig.orch.login_ok is True


async def test_auth_problem_survives_a_failed_non_auth_turn():
    rig = Rig(FakeBrain([[AuthProblem(), TurnEnd(False)], [TurnEnd(False)]]))
    await rig.say("調べて")
    await rig.say("もう一回")
    assert rig.orch.login_ok is False


async def test_stale_auth_flag_is_cleared_with_a_new_brain():
    rig = Rig(FakeBrain([[AuthProblem()]]))
    await rig.say("調べて")
    assert rig.orch._auth_seen is True
    rig.orch._brain = None
    rig.orch._ensure_brain()
    assert rig.orch._auth_seen is False


# ---- logs never carry what was said (they end up in `zundamon-jarvis doctor`) ----

def _log_text(caplog) -> str:
    out = []
    for r in caplog.records:
        out.append(r.getMessage())
        if r.exc_info:
            import traceback
            out.append("".join(traceback.format_exception(*r.exc_info)))
    return "\n".join(out)


async def test_speak_failure_log_has_no_text(caplog):
    import logging

    rig = Rig(FakeBrain([[TextDelta("秘密の返事です。"), TurnEnd()]]))

    async def broken(text):
        raise RuntimeError(f"http://127.0.0.1:50021/audio_query?text={text}")

    rig.orch._speak = broken
    with caplog.at_level(logging.INFO, logger="zundamon_jarvis.orchestrator"):
        await rig.say("こんにちは")
        await settle()
    msgs = _log_text(caplog)
    assert "speak failed" in msgs and "秘密" not in msgs


async def test_failed_event_and_unanswered_confirmation_logs_have_no_text(caplog, monkeypatch):
    import logging

    decisions: list[bool] = []
    rig = Rig(FakeBrain([[calendar_request(decisions)]]))
    with caplog.at_level(logging.INFO, logger="zundamon_jarvis.orchestrator"):
        await rig.say("ジムの予定を入れて")
        await rig.clock.advance(20)
        monkeypatch.setattr(rig.orch, "_on_event", lambda ev: 1 / 0)
        rig.brain.emit(TextDelta("内緒の話"))
        await settle()
        await rig.orch.pause()
        await rig.orch.on_utterance("聞かれたくない独り言")
    msgs = _log_text(caplog)
    assert "no answer to the confirmation" in msgs and "failed to handle TextDelta" in msgs
    assert "dropped (paused)" in msgs
    for secret in ("ジム", "内緒", "独り言", QUESTION):
        assert secret not in msgs
