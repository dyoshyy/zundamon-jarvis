import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from zundamon_jarvis.convlog import (
    ConversationLog,
    legacy_log_dir,
    log_dir,
    logged_speak,
    logged_transcribe,
    migrate_legacy_logs,
    prune,
)

JST = timezone(timedelta(hours=9))


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def make_log(tmp_path, when=datetime(2026, 10, 24, 10, 0, 0, tzinfo=JST)):
    return ConversationLog(tmp_path, now=lambda: when)


# ---- rotation ----

def test_prune_keeps_14_days_and_ignores_other_files(tmp_path):
    today = date(2026, 10, 24)
    for days in range(0, 20):
        (tmp_path / f"{today - timedelta(days=days)}.jsonl").write_text("{}\n")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "weird.jsonl").write_text("x")
    removed = prune(tmp_path, today)
    left = sorted(p.name for p in tmp_path.glob("*.jsonl") if p.name != "weird.jsonl")
    assert left[0] == "2026-10-11.jsonl" and left[-1] == "2026-10-24.jsonl"
    assert len(left) == 14
    assert len(removed) == 6
    assert (tmp_path / "notes.txt").exists() and (tmp_path / "weird.jsonl").exists()


def test_prune_on_a_missing_dir_is_a_noop(tmp_path):
    assert prune(tmp_path / "nope", date(2026, 10, 24)) == []


# ---- one line per utterance ----

def test_heard_then_replied_is_one_line_when_back_to_listening(tmp_path):
    log = make_log(tmp_path)
    log.heard("明日の予定は？", "listening")
    log.on_state("thinking")
    log.on_state("speaking")
    log.replied("十時から会議です。")
    log.replied("午後は空いてます。")
    assert not list(tmp_path.glob("*.jsonl"))  # not written mid-turn
    log.on_state("listening")
    assert lines(tmp_path / "2026-10-24.jsonl") == [{
        "ts": "2026-10-24T10:00:00+09:00",
        "heard": "明日の予定は？",
        "replied": "十時から会議です。午後は空いてます。",
        "state": "listening",
    }]


def test_heard_with_no_reply_is_written_after_the_turn(tmp_path):
    log = make_log(tmp_path)
    log.heard("あれ、なんだっけ", "listening")
    log.on_state("thinking")
    log.on_state("listening")  # [[silent]]
    assert lines(tmp_path / "2026-10-24.jsonl")[0]["replied"] == ""


def test_next_utterance_flushes_the_previous_one(tmp_path):
    log = make_log(tmp_path)
    log.heard("はい", "listening")  # e.g. a confirmation answer: no state change
    log.heard("次の話", "listening")
    log.close()
    assert [x["heard"] for x in lines(tmp_path / "2026-10-24.jsonl")] == ["はい", "次の話"]


def test_unsolicited_reply_gets_its_own_line(tmp_path):
    log = make_log(tmp_path)
    log.on_state("speaking")
    log.replied("調べ終わったよ。")
    log.on_state("listening")
    assert lines(tmp_path / "2026-10-24.jsonl") == [{
        "ts": "2026-10-24T10:00:00+09:00", "heard": "", "replied": "調べ終わったよ。",
        "state": "speaking",
    }]


def test_a_state_change_before_any_activity_does_not_cut_the_line(tmp_path):
    log = make_log(tmp_path)
    log.heard("ねえ", "speaking")  # barge-in: heard while still speaking
    log.on_state("listening")  # the old reply stopping
    assert not list(tmp_path.glob("*.jsonl"))
    log.on_state("thinking")
    log.replied("うん。")
    log.on_state("listening")
    assert lines(tmp_path / "2026-10-24.jsonl")[0]["replied"] == "うん。"


def test_close_writes_what_is_pending(tmp_path):
    log = make_log(tmp_path)
    log.heard("おやすみ", "listening")
    log.close()
    assert lines(tmp_path / "2026-10-24.jsonl")[0]["heard"] == "おやすみ"


def test_log_dir_is_created(tmp_path):
    log = ConversationLog(tmp_path / "a" / "b", now=lambda: datetime(2026, 1, 2, tzinfo=JST))
    log.heard("x", "listening")
    log.close()
    assert (tmp_path / "a" / "b" / "2026-01-02.jsonl").exists()


# ---- migration from the pre-rename directory ----

def test_log_dirs_follow_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_dir() == tmp_path / "zundamon-jarvis"
    assert legacy_log_dir() == tmp_path / "jarvis"


def test_migrate_moves_the_legacy_logs_and_removes_the_empty_dir(tmp_path):
    legacy = tmp_path / "jarvis"
    legacy.mkdir()
    (legacy / "2026-10-23.jsonl").write_text('{"heard": "a"}\n')
    (legacy / "2026-10-24.jsonl").write_text('{"heard": "b"}\n')
    new = tmp_path / "zundamon-jarvis"
    moved = migrate_legacy_logs(new, legacy)
    assert sorted(p.name for p in moved) == ["2026-10-23.jsonl", "2026-10-24.jsonl"]
    assert (new / "2026-10-24.jsonl").read_text() == '{"heard": "b"}\n'
    assert not legacy.exists()


def test_migrate_never_overwrites_a_log_already_in_the_new_dir(tmp_path):
    legacy = tmp_path / "jarvis"
    legacy.mkdir()
    (legacy / "2026-10-24.jsonl").write_text("old\n")
    (legacy / "notes.txt").write_text("keep me\n")
    new = tmp_path / "zundamon-jarvis"
    new.mkdir()
    (new / "2026-10-24.jsonl").write_text("new\n")
    assert migrate_legacy_logs(new, legacy) == []
    assert (new / "2026-10-24.jsonl").read_text() == "new\n"
    assert (legacy / "2026-10-24.jsonl").exists() and (legacy / "notes.txt").exists()


def test_migrate_without_a_legacy_dir_is_a_noop(tmp_path):
    new = tmp_path / "zundamon-jarvis"
    assert migrate_legacy_logs(new, tmp_path / "missing") == []
    assert not new.exists()


# ---- wrappers for the orchestrator's transcribe / speak ----

async def test_logged_transcribe_reports_on_the_loop_before_returning():
    loop = asyncio.get_running_loop()
    seen = []
    fn = logged_transcribe(lambda audio: f"text-{audio}", loop, seen.append)
    text = await asyncio.to_thread(fn, 7)
    assert text == "text-7"
    assert seen == ["text-7"]  # delivered before the awaiting coroutine resumed


async def test_logged_speak_records_then_speaks():
    order = []

    async def speak(text):
        order.append(("speak", text))
        return True

    fn = logged_speak(speak, lambda t: order.append(("log", t)))
    assert await fn("こんにちは。") is True
    assert order == [("log", "こんにちは。"), ("speak", "こんにちは。")]
