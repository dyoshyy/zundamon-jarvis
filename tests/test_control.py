import asyncio
import json
import os
import stat

import pytest

from helpers import WINDOWS, endpoint, open_endpoint
from zundamon_jarvis import control, transport


class Target:
    def __init__(self, state="listening"):
        self.state = state
        self.calls = []

    async def pause(self):
        self.calls.append("pause")
        self.state = "paused"

    async def resume(self):
        self.calls.append("resume")
        self.state = "listening"

    async def toggle(self):
        self.calls.append("toggle")
        self.state = "listening" if self.state == "paused" else "paused"


async def test_status_reports_state_and_text():
    t = Target("thinking")
    assert await control.handle("status\n", t) == {"state": "thinking", "text": "Thinking"}
    assert t.calls == []


@pytest.mark.parametrize("state, text", [
    ("listening", "Listening"), ("paused", "Paused"), ("thinking", "Thinking"),
    ("speaking", "Speaking"), ("limit", "Limit"), ("offline", "Offline"),
])
def test_every_state_has_a_short_text(state, text):
    assert control.status_of(state) == {"state": state, "text": text}


async def test_commands_act_then_return_the_new_status():
    t = Target("listening")
    assert await control.handle("toggle", t) == {"state": "paused", "text": "Paused"}
    assert await control.handle("resume\n", t) == {"state": "listening", "text": "Listening"}
    assert await control.handle(" pause ", t) == {"state": "paused", "text": "Paused"}
    assert t.calls == ["toggle", "resume", "pause"]


async def test_unknown_command_is_an_error_and_does_nothing():
    t = Target()
    resp = await control.handle("explode", t)
    assert "error" in resp and "explode" in resp["error"]
    assert t.calls == []


def test_socket_path_is_in_the_runtime_dir(monkeypatch, tmp_path):
    if WINDOWS:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert control.socket_path() == tmp_path / "zundamon-jarvis" / "run" / "control.json"
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert control.socket_path() == tmp_path / "zundamon-jarvis.sock"


async def test_server_round_trip_over_the_socket(tmp_path):
    path = endpoint(tmp_path)
    t = Target("listening")
    server = await control.serve(t, path)
    try:
        if not WINDOWS:  # no mode bits there: the TCP token guards the endpoint instead
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        resp = await asyncio.to_thread(control.request, "toggle", path)
        assert resp == {"state": "paused", "text": "Paused"}
        resp = await asyncio.to_thread(control.request, "status", path)
        assert resp == {"state": "paused", "text": "Paused"}
    finally:
        server.close()
        await server.wait_closed()


async def test_server_replaces_a_stale_socket_but_refuses_a_live_one(tmp_path):
    path = endpoint(tmp_path)
    path.write_text("", encoding="utf-8")  # stale leftover from a crash
    server = await control.serve(Target(), path)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await control.serve(Target(), path)
    finally:
        server.close()
        await server.wait_closed()


def test_request_without_a_daemon_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        control.request("status", endpoint(tmp_path, "nobody"))


async def test_reply_is_one_json_line(tmp_path):
    path = endpoint(tmp_path)
    server = await control.serve(Target("speaking"), path)
    try:
        reader, writer = await open_endpoint(path)
        writer.write(b"status\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw) == {"state": "speaking", "text": "Speaking"}


async def test_a_bare_liveness_probe_is_not_an_error(tmp_path, caplog):
    path = endpoint(tmp_path)
    t = Target()
    server = await control.serve(t, path)
    try:
        assert await asyncio.to_thread(control.is_live, path)
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()
    assert "control request failed" not in caplog.text and "unknown" not in caplog.text
    assert t.calls == []


@pytest.mark.parametrize("reasons, text", [
    ((), "Offline"), (("mic",), "Offline (mic)"), (("tts",), "Offline (voice)"),
    (("tts", "mic"), "Offline (mic, voice)"),
])
async def test_offline_shows_a_short_english_reason(reasons, text):
    t = Target("offline")
    t.offline_reasons = set(reasons)
    assert await control.handle("status", t) == {"state": "offline", "text": text}


async def test_reasons_only_show_while_offline():
    t = Target("listening")
    t.offline_reasons = {"mic"}
    assert (await control.handle("status", t))["text"] == "Listening"


# ---- watch: history, then live events, to any number of watchers ----

from datetime import datetime, timedelta, timezone  # noqa: E402

JST = timezone(timedelta(hours=9))


def fixed_now():
    return datetime(2026, 10, 24, 12, 34, 56, tzinfo=JST)


async def open_watch(path):
    reader, writer = await open_endpoint(path, limit=2**22)
    writer.write(b"watch\n")
    await writer.drain()
    return reader, writer


async def read_event(reader, timeout=1.0):
    line = await asyncio.wait_for(reader.readline(), timeout)
    assert line, "connection closed"
    return json.loads(line)


@pytest.fixture
async def watch_server(tmp_path):
    hub = control.WatchHub(now=fixed_now)
    path = endpoint(tmp_path)
    server = await control.serve(Target(), path, hub=hub)
    yield hub, path
    hub.close()
    server.close()
    await asyncio.wait_for(server.wait_closed(), 2)


def test_publish_stamps_id_time_and_kind():
    hub = control.WatchHub(now=fixed_now)
    ev = hub.publish("heard", text="明日の予定は？")
    assert ev == {"id": 1, "ts": "2026-10-24T12:34:56+09:00", "kind": "heard", "text": "明日の予定は？"}
    assert hub.publish("said", text="はい。", what="reply")["id"] == 2


async def test_watch_sends_hello_history_live_marker_then_live_events(watch_server):
    hub, path = watch_server
    hub.publish("heard", text="一つ目")
    hub.publish("said", text="うん。", what="reply")
    reader, writer = await open_watch(path)
    hello = await read_event(reader)
    assert hello == {"kind": "hello", "boot": hub.boot}
    assert [(await read_event(reader))["text"] for _ in range(2)] == ["一つ目", "うん。"]
    assert await read_event(reader) == {"kind": "live"}
    hub.publish("tool", label="📖 Read file: a.md", sub=False)
    ev = await read_event(reader)
    assert ev["kind"] == "tool" and ev["label"] == "📖 Read file: a.md" and ev["id"] == 3
    writer.close()


async def test_live_only_events_reach_watchers_but_not_the_history(watch_server):
    hub, path = watch_server
    reader, writer = await open_watch(path)
    await read_event(reader)  # hello
    assert await read_event(reader) == {"kind": "live"}
    ev = hub.publish_live("hearing", on=True)
    assert ev["kind"] == "hearing" and ev["on"] is True and ev["id"] == 1
    assert (await read_event(reader)) == ev
    writer.close()
    reader2, writer2 = await open_watch(path)
    await read_event(reader2)  # hello
    assert await read_event(reader2) == {"kind": "live"}  # nothing replayed
    writer2.close()


async def test_history_keeps_only_the_latest_events(tmp_path):
    hub = control.WatchHub(history=3, now=fixed_now)
    path = endpoint(tmp_path)
    server = await control.serve(Target(), path, hub=hub)
    try:
        for i in range(10):
            hub.publish("heard", text=str(i))
        reader, writer = await open_watch(path)
        await read_event(reader)  # hello
        assert [(await read_event(reader)).get("text") for _ in range(4)] == ["7", "8", "9", None]
        writer.close()
    finally:
        hub.close()
        server.close()
        await server.wait_closed()


async def test_every_watcher_gets_live_events(watch_server):
    hub, path = watch_server
    watchers = [await open_watch(path) for _ in range(3)]
    for reader, _ in watchers:
        assert (await read_event(reader))["kind"] == "hello"
        assert (await read_event(reader))["kind"] == "live"
    hub.publish("heard", text="みんなへ")
    for reader, writer in watchers:
        assert (await read_event(reader))["text"] == "みんなへ"
        writer.close()


async def test_a_watcher_that_leaves_is_forgotten(watch_server):
    hub, path = watch_server
    reader, writer = await open_watch(path)
    await read_event(reader)
    await read_event(reader)
    assert hub.watchers == 1
    writer.close()
    for _ in range(100):
        if hub.watchers == 0:
            break
        await asyncio.sleep(0.01)
    assert hub.watchers == 0
    hub.publish("heard", text="誰もいない")  # nothing to write to: fine


async def test_a_stuck_watcher_is_dropped_without_blocking_the_others(tmp_path):
    hub = control.WatchHub(queue_max=8, now=fixed_now)
    path = endpoint(tmp_path)
    server = await control.serve(Target(), path, hub=hub)
    try:
        slow = transport.connect(path, 2)  # never reads a byte
        slow.sendall(b"watch\n")
        fast_reader, fast_writer = await open_watch(path)
        await asyncio.sleep(0.05)
        assert hub.watchers == 2
        got: list[int] = []

        async def read_fast():
            while True:
                ev = await read_event(fast_reader, timeout=5)
                if ev.get("kind") == "heard":
                    got.append(ev["id"])

        fast = asyncio.create_task(read_fast())
        big = "あ" * 50_000
        loop = asyncio.get_running_loop()
        for _ in range(60):
            t0 = loop.time()
            hub.publish("heard", text=big)
            assert loop.time() - t0 < 0.05  # publishing never waits for a watcher
            await asyncio.sleep(0.01)
        for _ in range(200):
            if len(got) == 60:
                break
            await asyncio.sleep(0.01)
        assert len(got) == 60  # the reading watcher missed nothing
        assert hub.watchers == 1  # the stuck one was dropped
        fast.cancel()
        slow.close()
        fast_writer.close()
    finally:
        hub.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 2)


async def test_closing_the_hub_ends_every_watch_so_the_server_can_close(tmp_path):
    hub = control.WatchHub(now=fixed_now)
    path = endpoint(tmp_path)
    server = await control.serve(Target(), path, hub=hub)
    reader, _ = await open_watch(path)
    await read_event(reader)
    await read_event(reader)
    hub.close()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    server.close()
    await asyncio.wait_for(server.wait_closed(), 1)
    assert hub.watchers == 0


async def test_watch_without_a_hub_is_an_error(tmp_path):
    path = endpoint(tmp_path)
    server = await control.serve(Target(), path)
    try:
        resp = await asyncio.to_thread(control.request, "watch", path)
        assert "error" in resp
    finally:
        server.close()
        await server.wait_closed()


def test_a_bad_value_does_not_break_publish():
    hub = control.WatchHub(now=fixed_now)
    assert hub.publish("heard", text=object()) is None  # not JSON: logged and dropped
    assert hub.publish("heard", text="ok")["id"] >= 1


# ---- settings: get / set / preview / newsession ----

class FakeSettings:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"speed": 1.2, "speaker_id": 13, "model": "sonnet"}

    async def set(self, key, raw):
        self.calls.append(("set", key, raw))
        if key == "speed" and raw == "1.5":
            return {"ok": True, "saved": True, "settings": {**self.snapshot(), "speed": 1.5}}
        return {"error": "bad"}

    async def preview(self, speaker, speed, text):
        self.calls.append(("preview", speaker, speed, text))
        return {"ok": True}

    def new_session(self):
        self.calls.append(("newsession",))
        return {"session": "closed", "settings": self.snapshot()}


async def test_get_returns_the_status_and_the_settings():
    t, s = Target("paused"), FakeSettings()
    resp = await control.handle("get\n", t, s)
    assert resp == {"state": "paused", "text": "Paused",
                    "settings": {"speed": 1.2, "speaker_id": 13, "model": "sonnet"}}


async def test_set_passes_key_and_value_and_adds_the_status():
    t, s = Target(), FakeSettings()
    resp = await control.handle("set speed 1.5\n", t, s)
    assert resp["state"] == "listening" and resp["settings"]["speed"] == 1.5
    assert s.calls == [("set", "speed", "1.5")]
    assert "error" in await control.handle("set speed 9", t, s)


@pytest.mark.parametrize("line", ["set", "set speed", "set  ", "preview", "preview 3",
                                  "preview 3 1.2", "get extra", "newsession now"])
async def test_malformed_settings_commands_are_errors(line):
    s = FakeSettings()
    resp = await control.handle(line, Target(), s)
    assert "error" in resp and s.calls == []


async def test_preview_keeps_the_text_whole():
    t, s = Target(), FakeSettings()
    assert (await control.handle("preview 3 1.2 この速さで 話すね。\n", t, s))["ok"] is True
    assert s.calls == [("preview", "3", "1.2", "この速さで 話すね。")]


async def test_newsession():
    t, s = Target(), FakeSettings()
    resp = await control.handle("newsession", t, s)
    assert resp["session"] == "closed" and resp["state"] == "listening"


async def test_settings_commands_without_settings_are_errors():
    for line in ("get", "set speed 1.5", "preview 3 1.2 a", "newsession"):
        assert "error" in await control.handle(line, Target())


async def test_settings_round_trip_over_the_socket(tmp_path):
    path = endpoint(tmp_path)
    s = FakeSettings()
    server = await control.serve(Target(), path, settings=s)
    try:
        resp = await asyncio.to_thread(control.request, "set speed 1.5", path)
        assert resp["settings"]["speed"] == 1.5
        resp = await asyncio.to_thread(control.request, "preview 3 1.2 やあ", path)
        assert resp == {"state": "listening", "text": "Listening", "ok": True}
    finally:
        server.close()
        await server.wait_closed()


# ---- debug ----

async def test_debug_returns_the_status_and_the_daemon_diagnostics():
    t = Target("listening")
    t.debug = lambda: {"ears_muted": False, "frames_last_5s": 156}
    resp = await control.handle("debug\n", t)
    assert resp == {"state": "listening", "text": "Listening",
                    "debug": {"ears_muted": False, "frames_last_5s": 156}}


async def test_debug_without_diagnostics_is_an_error():
    assert "error" in await control.handle("debug", Target())


async def test_status_adds_login_false_only_when_logged_out():
    t = Target("listening")
    assert "login" not in await control.handle("status", t)
    t.login_ok = False
    assert await control.handle("status", t) == {"state": "listening", "text": "Listening", "login": False}
