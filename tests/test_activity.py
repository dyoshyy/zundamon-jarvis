from zundamon_jarvis.activity import ActivityFeed
from zundamon_jarvis.brain import TaskDone, TextDelta, ToolUse, TurnEnd


class Sink(list):
    def __call__(self, kind, **fields):
        self.append((kind, fields))


def feed(reasons=()):
    sink = Sink()
    return ActivityFeed(sink, offline_reasons=lambda: set(reasons)), sink


def test_heard_and_said():
    f, sink = feed()
    f.heard("明日の予定は？")
    f.said("明日はジムだよ。", "reply")
    assert sink == [("heard", {"text": "明日の予定は？"}),
                    ("said", {"text": "明日はジムだよ。", "what": "reply"})]


def test_hearing_goes_to_the_live_only_publisher():
    sink, live = Sink(), Sink()
    f = ActivityFeed(sink, publish_live=live)
    f.hearing(True)
    f.hearing(False)
    assert live == [("hearing", {"on": True}), ("hearing", {"on": False})] and sink == []


def test_tool_use_becomes_a_label_never_the_input():
    f, sink = feed()
    f.brain_event(ToolUse("Write", {"file_path": "/a/memo.md", "content": "秘密"}, None))
    f.brain_event(ToolUse("Read", {"file_path": "/a/x.md"}, "toolu_agent"))
    assert sink == [
        ("tool", {"label": "📝 Save note: memo.md", "labels": {"ja": "📝 メモを保存: memo.md", "en": "📝 Save note: memo.md"},
                  "sub": False}),
        ("tool", {"label": "📖 Read file: x.md", "labels": {"ja": "📖 ファイルを読む: x.md", "en": "📖 Read file: x.md"},
                  "sub": True}),
    ]
    assert "秘密" not in repr(sink)


def test_task_done_is_reported_with_a_short_summary():
    f, sink = feed()
    f.brain_event(TaskDone("t1", "completed", "数え終わりました。" + "あ" * 200))
    ((kind, fields),) = sink
    assert kind == "task" and fields["status"] == "completed"
    assert fields["summary"].startswith("数え終わりました。") and len(fields["summary"]) <= 60


def test_other_brain_events_are_not_published():
    f, sink = feed()
    f.brain_event(TextDelta("うん"))
    f.brain_event(TurnEnd())
    assert sink == []


def test_confirm_outcome():
    f, sink = feed()
    f.confirm("予定を入れるけど、いい？", True, "answer")
    assert sink == [("confirm", {"question": "予定を入れるけど、いい？", "ok": True, "why": "answer"})]


def test_state_is_compact_busy_states_are_not_published():
    f, sink = feed()
    for s in ["listening", "thinking", "speaking", "listening", "thinking", "paused", "paused",
              "listening", "speaking", "limit", "listening"]:
        f.state(s)
    states = [(k, f) for k, f in sink if k == "state"]
    assert [fields["state"] for _, fields in states] == [
        "listening", "paused", "listening", "limit", "listening"]
    assert states[1] == ("state", {"state": "paused", "text": "Paused"})


def test_offline_state_shows_its_reason():
    f, sink = feed(reasons=("mic",))
    f.state("offline")
    assert sink == [("state", {"state": "offline", "text": "Offline (mic)", "reasons": ["mic"]})]


def test_a_failing_sink_never_raises():
    def boom(kind, **fields):
        raise RuntimeError("x")

    f = ActivityFeed(boom)
    f.heard("a")
    f.state("paused")
    f.brain_event(ToolUse("Read", {}, None))


def test_thinking_is_published_as_on_off_pairs():
    f, sink = feed()
    for s in ["listening", "thinking", "thinking", "speaking", "listening", "thinking", "paused"]:
        f.state(s)
    assert [fields["on"] for kind, fields in sink if kind == "thinking"] == [True, False, True, False]


def test_auth_problem_is_ignored_by_the_feed():
    from zundamon_jarvis.brain import AuthProblem
    f, sink = feed()
    f.brain_event(AuthProblem())
    assert sink == []
