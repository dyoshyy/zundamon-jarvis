import json
from pathlib import Path

import pytest

from helpers import WINDOWS, endpoint
from zundamon_jarvis import ctl


def run(monkeypatch, capsys, argv, response=None, error=None):
    sent = []

    def fake_request(cmd, path=None, timeout=3.0):
        sent.append(cmd)
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(ctl, "request", fake_request)
    code = ctl.main(argv)
    return code, capsys.readouterr(), sent


def test_status_prints_the_daemon_reply(monkeypatch, capsys):
    code, out, sent = run(monkeypatch, capsys, ["status"], {"state": "paused", "text": "Paused"})
    assert code == 0 and sent == ["status"]
    assert json.loads(out.out) == {"state": "paused", "text": "Paused"}


def test_toggle_sends_toggle(monkeypatch, capsys):
    code, _, sent = run(monkeypatch, capsys, ["toggle"], {"state": "listening", "text": "Listening"})
    assert code == 0 and sent == ["toggle"]


def test_no_daemon_is_reported_as_off(monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, ["status"], error=FileNotFoundError())
    assert code == 1
    assert json.loads(out.out) == {"state": "off", "text": "Stopped"}


def test_bad_usage(monkeypatch, capsys):
    code, out, sent = run(monkeypatch, capsys, ["dance"], {})
    assert code == 2 and sent == [] and "usage" in out.err


def test_bar_shows_icon_and_text(monkeypatch, capsys):
    code, out, sent = run(monkeypatch, capsys, ["bar"], {"state": "paused", "text": "Paused"})
    data = json.loads(out.out)
    assert code == 0 and sent == ["status"]
    assert "Paused" in data["text"] and data["class"] == "paused"
    assert data["tooltip"] == ("ZundamonJarvis: Paused — click: conversation, right-click: settings, "
                               "SUPER+ALT+J: pause/resume")


def test_bar_prints_nothing_without_a_daemon(monkeypatch, capsys):
    # The Omarchy bar shows the raw output when "text" is empty, so print nothing at all.
    code, out, _ = run(monkeypatch, capsys, ["bar"], error=ConnectionRefusedError())
    assert code == 0 and out.out == ""


# ---- watch: formatting ----

import asyncio  # noqa: E402
import io  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402


from zundamon_jarvis import control  # noqa: E402

TS = "2026-10-24T09:05:30+09:00"


def ev(kind, **fields):
    return {"id": 1, "ts": TS, "kind": kind, **fields}


@pytest.mark.parametrize("event, line", [
    (ev("heard", text="明日の予定は？"), "09:05 You ▸\n  明日の予定は？"),
    (ev("said", text="明日はジムだよ。", what="reply"), "09:05 ZundamonJarvis ▸\n  明日はジムだよ。"),
    (ev("said", text="ちょっと待ってね", what="filler"), "09:05 ZundamonJarvis ▸\n  (ちょっと待ってね)"),
    (ev("said", text="上限に達したから黙るね", what="notice"), "09:05 ZundamonJarvis ▸\n  上限に達したから黙るね"),
    (ev("said", text="予定を入れるけど、いい？", what="question"), "09:05 ❓ Confirm: 予定を入れるけど、いい？"),
    (ev("confirm", question="q", ok=True, why="answer"), "09:05 ✅ Yes → go"),
    (ev("confirm", question="q", ok=False, why="answer"), "09:05 ❌ No → cancelled"),
    (ev("confirm", question="q", ok=False, why="timeout"), "09:05 ⌛ No answer → cancelled"),
    (ev("confirm", question="q", ok=False, why="unclear"), "09:05 ❔ Not understood → cancelled"),
    (ev("tool", label="📅 Check calendar", sub=False), "09:05   📅 Check calendar"),
    (ev("tool", label="📖 Read file: x.md", sub=True), "09:05   bg: 📖 Read file: x.md"),
    (ev("state", state="paused", text="Paused"), "09:05 ⏸ Paused"),
    (ev("state", state="listening", text="Listening"), "09:05 ▶ Listening"),
    (ev("state", state="limit", text="Limit"), "09:05 ⛔ Limit"),
    (ev("state", state="offline", text="Offline (mic)"), "09:05 📴 Offline (mic)"),
    (ev("task", status="completed", summary="数え終わった"), "09:05 🧵 Background task finished: 数え終わった"),
    (ev("task", status="failed", summary=""), "09:05 🧵 Background task failed"),
    (ev("task", status="stopped", summary=""), "09:05 🧵 Background task stopped"),
])
def test_format_event(event, line):
    assert ctl.format_event(event, color=False) == line


def test_format_unknown_or_broken_events_is_safe():
    assert ctl.format_event(ev("mystery"), color=False) is None
    assert ctl.format_event({"kind": "heard"}, color=False) == "--:-- You ▸\n  "
    assert ctl.format_event({"kind": "heard", "ts": "garbage", "text": "x"}, color=False) == "--:-- You ▸\n  x"


def test_color_wraps_lines_and_resets():
    line = ctl.format_event(ev("heard", text="x"), color=True)
    assert "\033[" in line and line.endswith("\033[0m") and "x" in line


def test_view_skips_what_it_has_already_shown_after_a_reconnect():
    view = ctl.WatchView(color=False)
    first = [{"kind": "hello", "boot": "b1"}, {**ev("heard", text="a"), "id": 1},
             {"kind": "live"}, {**ev("heard", text="b"), "id": 2}]
    out = [line for e in first for line in view.feed(e)]
    assert out == ["09:05 You ▸\n  a", ctl.LIVE_LINE, "09:05 You ▸\n  b"]
    assert view.disconnected() == ctl.WAITING_LINE
    assert view.disconnected() is None  # said once while waiting
    again = [{"kind": "hello", "boot": "b1"}, {**ev("heard", text="a"), "id": 1},
             {**ev("heard", text="b"), "id": 2}, {"kind": "live"}, {**ev("heard", text="c"), "id": 3}]
    assert [line for e in again for line in view.feed(e)] == ["09:05 You ▸\n  c"]


def test_view_shows_everything_again_after_a_daemon_restart():
    view = ctl.WatchView(color=False)
    for e in [{"kind": "hello", "boot": "b1"}, {**ev("heard", text="a"), "id": 5}, {"kind": "live"}]:
        view.feed(e)
    view.disconnected()
    lines = [line for e in [{"kind": "hello", "boot": "b2"}, {**ev("heard", text="new"), "id": 1},
                            {"kind": "live"}] for line in view.feed(e)]
    assert lines == [ctl.RESTART_LINE, "09:05 You ▸\n  new", ctl.LIVE_LINE]


async def test_watch_prints_history_then_live_and_waits_when_the_daemon_goes(tmp_path):
    hub = control.WatchHub()
    path = endpoint(tmp_path)
    server = await control.serve(object(), path, hub=hub)
    hub.publish("heard", text="前の話")
    out = io.StringIO()
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        raise KeyboardInterrupt  # Ctrl-C while waiting for the daemon

    done = threading.Event()
    result = {}

    def run():
        result["code"] = ctl.watch(path, out=out, sleep=fake_sleep, color=False)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    for _ in range(200):
        if hub.watchers:
            break
        await asyncio.sleep(0.01)
    hub.publish("tool", label="📅 Check calendar", sub=False)
    hub.publish("said", text="明日はジムだよ。", what="reply")
    await asyncio.sleep(0.2)
    hub.close()
    server.close()
    await server.wait_closed()
    assert await asyncio.to_thread(done.wait, 2)
    lines = out.getvalue().splitlines()
    assert lines[0][6:] == "You ▸" and lines[1] == "  前の話"
    assert lines[2:] == [ctl.LIVE_LINE, lines[3], lines[4], lines[5], ctl.WAITING_LINE]
    assert lines[3].endswith("  📅 Check calendar") and lines[4].endswith("ZundamonJarvis ▸")
    assert lines[5] == "  明日はジムだよ。"
    assert result["code"] == 0 and sleeps == [ctl.RETRY_S]


def test_watch_without_a_daemon_says_so_and_keeps_waiting(tmp_path):
    out = io.StringIO()
    calls = []

    def fake_sleep(s):
        calls.append(s)
        if len(calls) == 3:
            raise KeyboardInterrupt

    assert ctl.watch(endpoint(tmp_path, "none"), out=out, sleep=fake_sleep, color=False) == 0
    assert out.getvalue().splitlines() == [ctl.WAITING_LINE]
    assert len(calls) == 3


def test_main_watch_runs_the_client(monkeypatch):
    called = []
    monkeypatch.setattr(ctl, "watch", lambda: called.append(1) or 0)
    assert ctl.main(["watch"]) == 0 and called == [1]


# ---- window (the opening logic is tested in test_desktop.py) ----

def test_main_window_toggles_the_watch_window(monkeypatch):
    got = []
    monkeypatch.setattr(ctl.desktop, "toggle_window", lambda name, argv: got.append((name, argv)))
    assert ctl.main(["window"]) == 0
    assert ctl.main(["window", "watch"]) == 0
    name, argv = got[0]
    assert name == "watch" and Path(argv[0]).stem == "zundamon-jarvisctl" and argv[1:] == ["watch"]
    assert got[0] == got[1]
    assert ctl.main(["window", "nope"]) == 2


def test_main_window_settings_opens_the_settings_window(monkeypatch):
    got = []
    monkeypatch.setattr(ctl.desktop, "toggle_window", lambda name, argv: got.append((name, argv)))
    assert ctl.main(["window", "settings"]) == 0
    name, argv = got[0]
    assert name == "settings" and Path(argv[0]).stem == "zundamon-jarvisctl" and argv[1:] == ["settings"]


# ---- settings from the command line ----

def test_get_prints_the_settings(monkeypatch, capsys):
    reply = {"state": "listening", "text": "Listening", "settings": {"speed": 1.2}}
    code, out, sent = run(monkeypatch, capsys, ["get"], reply)
    assert code == 0 and sent == ["get"] and json.loads(out.out) == reply


def test_set_sends_key_and_value(monkeypatch, capsys):
    code, out, sent = run(monkeypatch, capsys, ["set", "speed", "1.5"], {"ok": True})
    assert code == 0 and sent == ["set speed 1.5"]
    code, out, sent = run(monkeypatch, capsys, ["set", "speed", "9"], {"error": "range"})
    assert code == 2


def test_set_needs_exactly_key_and_value(monkeypatch, capsys):
    for argv in (["set"], ["set", "speed"], ["set", "speed", "1", "2"], ["get", "x"]):
        code, out, sent = run(monkeypatch, capsys, argv, {})
        assert code == 2 and sent == [], argv


def test_settings_runs_the_app_lazily(monkeypatch):
    import sys
    import types

    called = []
    fake = types.ModuleType("zundamon_jarvis.settings_app")
    fake.main = lambda: called.append(1) or 0
    monkeypatch.setitem(sys.modules, "zundamon_jarvis.settings_app", fake)
    assert ctl.main(["settings"]) == 0 and called == [1]


def test_zundamon_jarvisctl_does_not_load_the_tui_library():
    import sys

    code = "import sys, zundamon_jarvis.ctl; print('textual' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "False", out.stderr


@pytest.mark.skipif(WINDOWS, reason="the GTK character overlay runs under the Linux system python")
def test_character_runs_the_overlay_under_the_system_python():
    calls = []
    code = ctl._character("speaking", execve=lambda *a: calls.append(a))
    (path, cmd, env), = calls
    assert code == 0 and path == cmd[0] == ctl.SYSTEM_PYTHON
    assert cmd[1:] == ["-m", "zundamon_jarvis.character_app", "speaking"]
    assert env["PYTHONPATH"].endswith("src")


def test_character_knows_which_zundamon_jarvisctl_opens_the_watch_window():
    cmd, env = ctl.character_command()
    assert env["ZUNDAMON_JARVISCTL"] == ctl._zundamon_jarvisctl()


def test_hearing_events_are_not_shown_and_keep_the_spinner():
    view = ctl.WatchView(color=False)
    view.feed({"kind": "hello", "boot": "b1"})
    view.feed({"kind": "live"})
    view.feed({**ev("thinking", on=True), "id": 1})
    assert view.feed({**ev("hearing", on=True), "id": 2}) == []
    assert view.busy  # the user talking over a turn does not end it


def test_debug_prints_the_diagnostics(monkeypatch, capsys):
    reply = {"state": "listening", "text": "Listening", "debug": {"ears_muted": False}}
    code, out, sent = run(monkeypatch, capsys, ["debug"], reply)
    assert code == 0 and sent == ["debug"] and json.loads(out.out) == reply


def test_view_is_busy_only_live_and_until_the_next_event():
    view = ctl.WatchView(color=False)
    view.feed({"kind": "hello", "boot": "b1"})
    view.feed({**ev("thinking", on=True), "id": 1})
    assert not view.busy  # still replaying history
    assert view.feed({"kind": "live"}) == [ctl.LIVE_LINE]
    assert view.busy
    assert view.feed({**ev("said", text="やあ", what="reply"), "id": 2}) == ["09:05 ZundamonJarvis ▸\n  やあ"]
    assert not view.busy
    view.feed({**ev("thinking", on=True), "id": 3})
    assert view.busy
    view.feed({**ev("thinking", on=False), "id": 4})
    assert not view.busy
    view.feed({**ev("thinking", on=True), "id": 5})
    view.disconnected()
    assert not view.busy


def test_loader_draws_a_spinner_while_busy_and_scrolls_lines_above_it():
    out = io.StringIO()
    view = ctl.WatchView(color=False)
    view.feed({"kind": "live"})
    loader = ctl.Loader(out, view)
    loader.tick()
    assert out.getvalue() == ""  # idle: nothing to draw
    out.truncate(0); out.seek(0)
    view.feed({**ev("thinking", on=True), "id": 1})
    loader.tick()
    assert "thinking" in out.getvalue()
    out.truncate(0); out.seek(0)
    loader.line("hello")
    assert out.getvalue() == ctl.Loader.CLEAR + "hello\n"
    out.truncate(0); out.seek(0)
    view.feed({**ev("heard", text="x"), "id": 2})
    loader.tick()
    assert out.getvalue() == ""


def test_view_leaves_out_time_and_name_while_the_same_speaker_keeps_talking():
    view = ctl.WatchView(color=False)
    events = [ev("heard", text="a"), ev("heard", text="b"),
              ev("said", text="c", what="filler"), ev("said", text="d", what="reply"),
              ev("heard", text="e")]
    out = [line for i, e in enumerate(events, 1) for line in view.feed({**e, "id": i})]
    assert out == ["09:05 You ▸\n  a", "  b",
                   "09:05 ZundamonJarvis ▸\n  (c)", "  d",
                   "09:05 You ▸\n  e"]


def test_view_names_the_speaker_again_after_another_kind_of_line():
    view = ctl.WatchView(color=False)
    events = [ev("heard", text="a"), ev("tool", label="📅 Check calendar", sub=False),
              ev("heard", text="b")]
    out = [line for i, e in enumerate(events, 1) for line in view.feed({**e, "id": i})]
    assert out[2] == "09:05 You ▸\n  b"


def test_window_command_finds_the_windows_exe_beside_python(tmp_path, monkeypatch):
    (tmp_path / "zundamon-jarvisctl.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(ctl.sys, "executable", str(tmp_path / "python.exe"))
    assert ctl._zundamon_jarvisctl() == str(tmp_path / "zundamon-jarvisctl.exe")
