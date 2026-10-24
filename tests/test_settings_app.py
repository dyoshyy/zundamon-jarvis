"""The settings TUI: pure state logic, the client's offline fallback, and a Pilot smoke test."""

import pytest

from zundamon_jarvis import settings_app as sa
from zundamon_jarvis.config import load_config

SPEAKERS = [
    {"name": "四国めたん", "styles": [{"name": "ノーマル", "id": 2, "type": "talk"},
                                  {"name": "あまあま", "id": 0, "type": "talk"}]},
    {"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3, "type": "talk"},
                                  {"name": "ハミング", "id": 3001, "type": "frame_decode"}]},
    {"name": "青山龍星", "styles": [{"name": "ノーマル", "id": 13, "type": "talk"}]},
]


MICS = [sa.MicSource("cam", "Webcam"), sa.MicSource("bt", "Headset")]


# ---- pure logic ----


def test_mic_note_warns_about_bluetooth_headset_mics():
    v = sa.View(online=True, mic_device="bluez_input.AA:BB", mic_active="bluez_input.AA:BB")
    assert "call quality" in sa.mic_note(v)
    v = sa.View(online=True, mic_device="alsa_input.cam", mic_active="alsa_input.cam")
    assert "call quality" not in sa.mic_note(v)
    # following a Bluetooth default input is the same trap
    v = sa.View(online=True, mic_device=None, mic_active=None)
    assert "call quality" in sa.mic_note(v, default_name="bluez_input.AA")
    assert "call quality" not in sa.mic_note(v, default_name="alsa_input.cam")
    off = sa.View(online=False, mic_device="bluez_input.AA")
    assert sa.mic_note(off).startswith(sa.OFFLINE_NOTE) and "call quality" in sa.mic_note(off)

def test_mic_note():
    assert sa.mic_note(sa.View(online=False)) == "applies on next start"
    assert "Restart" in sa.mic_note(sa.View(online=True, mic_device="cam", mic_active=None))
    assert "Restart" not in sa.mic_note(sa.View(online=True, mic_device="cam", mic_active="cam"))

@pytest.mark.parametrize("speed, delta, want", [
    (1.2, +1, 1.3), (1.2, -1, 1.1), (2.0, +1, 2.0), (0.8, -1, 0.8), (1.95, +1, 2.0),
    (0.84, -1, 0.8), (1.0, +10, 2.0), (0.1, 0, 0.8), (1.25, 0, 1.3),
])
def test_step_speed_clamps_to_0_8_2_0_in_tenths(speed, delta, want):
    assert sa.step_speed(speed, delta) == want


def test_voice_groups_keep_engine_order_and_skip_singing():
    groups = sa.voice_groups(SPEAKERS)
    assert [g.name for g in groups] == ["四国めたん", "ずんだもん", "青山龍星"]
    assert [(v.style, v.id) for v in groups[0].voices] == [("ノーマル", 2), ("あまあま", 0)]
    assert [v.id for v in groups[1].voices] == [3]  # no singing style


def test_voice_label():
    groups = sa.voice_groups(SPEAKERS)
    assert sa.voice_label(groups, 0) == "四国めたん / あまあま"
    assert sa.voice_label(groups, 99) == "ID 99"
    assert sa.voice_label(groups, None) == "青山龍星 / ノーマル (default)"
    assert sa.voice_label([], 13) == "ID 13"


def test_cycle_model():
    assert sa.cycle_model("sonnet", +1) == "opus"
    assert sa.cycle_model("haiku", +1) == "sonnet"
    assert sa.cycle_model("sonnet", -1) == "haiku"
    assert sa.cycle_model(None, +1) == "sonnet"  # inherited: start from the first
    assert sa.cycle_model("claude-x", -1) == "sonnet"


def test_model_note():
    v = sa.View(online=True, model="opus", session_model="sonnet")
    assert "Sonnet" in sa.model_note(v) and "Switch now" in sa.model_note(v)
    assert sa.model_note(sa.View(online=True, model="opus", session_model="opus")) == ""
    assert sa.model_note(sa.View(online=True, model="opus")) == "applies from the next conversation"
    assert sa.model_note(sa.View(online=False, model="opus")) == "applies on next start"


def test_view_from_reply_and_from_config(tmp_path):
    v = sa.View.from_reply({"state": "paused", "text": "Paused", "settings": {
        "speed": 1.5, "speaker_id": 3, "model": "haiku", "session_model": "sonnet"}})
    assert (v.online, v.paused, v.speed, v.speaker_id, v.model, v.session_model) == \
        (True, True, 1.5, 3, "haiku", "sonnet")
    assert sa.View.from_reply({"settings": {"mic_device": "cam", "mic_active": 3}}).mic_active == 3
    p = tmp_path / "c.toml"
    p.write_text("speed = 1.4\nspeaker_id = 2\n")
    v = sa.View.from_config(load_config(p))
    assert (v.online, v.speed, v.speaker_id, v.model) == (False, 1.4, 2, "sonnet")


# ---- client: talks to the daemon, falls back to config.toml ----

class Daemon:
    def __init__(self, up=True):
        self.up = up
        self.sent = []
        self.settings = {"speed": 1.2, "speaker_id": 13, "model": "sonnet", "session_model": None}

    def request(self, cmd, path=None, timeout=3.0):
        if not self.up:
            raise ConnectionRefusedError()
        self.sent.append(cmd)
        status = {"state": "listening", "text": "Listening"}
        words = cmd.split()
        if words[0] == "set":
            if words[2] == "9":
                return {"error": "speed must be ..."}
            key = {"speaker": "speaker_id"}.get(words[1], words[1])
            self.settings[key] = float(words[2]) if key == "speed" else (
                int(words[2]) if key == "speaker_id" else words[2])
            return {**status, "ok": True, "saved": True, "settings": dict(self.settings)}
        if words[0] == "preview":
            return {**status, "ok": False, "busy": "speaking"}
        if words[0] == "newsession":
            return {**status, "session": "closed", "settings": dict(self.settings)}
        if words[0] == "toggle":
            return {"state": "paused", "text": "Paused", "settings": dict(self.settings)}
        return {**status, "settings": dict(self.settings)}


def test_client_online_set_goes_to_the_daemon(tmp_path):
    d = Daemon()
    c = sa.SettingsClient(request=d.request, config_path=tmp_path / "c.toml")
    view, msg = c.set("speed", 1.3)
    assert d.sent == ["set speed 1.3"] and view.speed == 1.3 and msg is None
    assert not (tmp_path / "c.toml").exists()  # the daemon writes it, not the window
    view, msg = c.set("speed", 9)
    assert msg and "speed" in msg


def test_client_offline_writes_the_config_and_says_so(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('mic_device = "m"\nother = "keep"\n')
    c = sa.SettingsClient(request=Daemon(up=False).request, config_path=path)
    view = c.load()
    assert not view.online and view.speed == 1.2
    view, msg = c.set("speed", 1.6)
    assert view.speed == 1.6 and "next start" in msg
    view, msg = c.set("speaker", 3)
    view, msg = c.set("model", "opus")
    cfg = load_config(path)
    assert (cfg.speed, cfg.speaker_id, cfg.model, cfg.mic_device) == (1.6, 3, "opus", "m")
    view, msg = c.set("mic", "cam")
    assert load_config(path).mic_device == "cam" and view.mic_device == "cam"
    view, msg = c.set("mic", "default")
    assert load_config(path).mic_device is None and "mic_device" not in path.read_text()
    assert 'other = "keep"' in path.read_text()
    view, msg = c.set("speed", 9)
    assert msg and load_config(path).speed == 1.6
    assert "not running" in c.preview(3, 1.2, "やあ")
    assert c.toggle()[1]


def test_client_preview_reports_busy(tmp_path):
    d = Daemon()
    c = sa.SettingsClient(request=d.request, config_path=tmp_path / "c.toml")
    assert "is speaking" in c.preview(3, 1.2, "この声で話すね。")
    assert d.sent == ["preview 3 1.2 この声で話すね。"]


# ---- the app (Textual Pilot) ----

class FakeClient:
    def __init__(self, online=True):
        self.view = sa.View(online=online, state="listening", state_text="Listening",
                            speed=1.2, speaker_id=13, model="sonnet")
        self.calls = []

    def load(self):
        return self.view

    def speakers(self):
        return SPEAKERS

    def mics(self):
        return MICS

    def default_mic(self):
        return "cam"

    def set(self, key, value):
        self.calls.append(("set", key, value))
        attr = {"speaker": "speaker_id", "mic": "mic_device"}.get(key, key)
        setattr(self.view, attr, None if value == "default" else value)
        return self.view, None

    def preview(self, sid, speed, text):
        self.calls.append(("preview", sid, speed, text))
        return None

    def toggle(self):
        self.calls.append(("toggle",))
        self.view.state = "listening" if self.view.state == "paused" else "paused"
        return self.view, None

    def new_session(self):
        self.calls.append(("newsession",))
        return self.view, "Conversation reset"


async def test_app_smoke_speed_voice_model_pause_and_close():
    client = FakeClient()
    app = sa.SettingsApp(client, preview_delay=0.05)
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.pause()
        # speed: → twice, then a sample at the new speed
        await pilot.press("right", "right")
        await pilot.pause(0.2)
        assert ("set", "speed", 1.3) in client.calls and ("set", "speed", 1.4) in client.calls
        assert ("preview", 13, 1.4, sa.SPEED_SAMPLE) in client.calls
        # voice: Enter opens the list; moving previews; Enter picks
        client.calls.clear()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, sa.VoiceScreen)
        await pilot.press("down")
        await pilot.pause(0.2)
        previews = [c for c in client.calls if c[0] == "preview"]
        assert previews and previews[-1][2] == 1.4 and previews[-1][3] == sa.VOICE_SAMPLE
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, sa.VoiceScreen)
        assert client.calls[-1] == ("set", "speaker", previews[-1][1])
        # model: → cycles
        await pilot.press("down", "right")
        await pilot.pause()
        assert ("set", "model", "opus") in client.calls
        # mic: → picks the first source, ← goes back to "System default"
        await pilot.press("down", "right")
        await pilot.pause()
        assert ("set", "mic", "cam") in client.calls
        await pilot.press("left")
        await pilot.pause()
        assert client.calls[-1] == ("set", "mic", "default")
        # pause row toggles
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert ("toggle",) in client.calls
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_code in (0, None)


async def test_app_voice_list_escape_changes_nothing():
    client = FakeClient()
    app = sa.SettingsApp(client, preview_delay=0.05)
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, sa.VoiceScreen)
        assert not [c for c in client.calls if c[0] == "set"]
        await pilot.press("q")
        await pilot.pause()


async def test_app_offline_shows_the_note():
    client = FakeClient(online=False)
    app = sa.SettingsApp(client, preview_delay=0.05)
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.pause()
        assert "next start" in str(app.query_one("#status").render())


async def test_polls_and_messages_while_the_voice_list_is_open_do_not_break_it():
    client = FakeClient()
    client.preview = lambda sid, speed, text: "ZundamonJarvis is speaking, so no preview"
    app = sa.SettingsApp(client, preview_delay=0.02, poll_s=0.05)
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause(0.3)  # several polls + a busy preview reply
        assert isinstance(app.screen, sa.VoiceScreen)
        assert "speaking" in str(app.screen.query_one("#msg").render())
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert "speaking" in str(app.query_one("#msg").render())
        await pilot.press("escape")


def test_view_from_a_garbled_reply_keeps_defaults():
    v = sa.View.from_reply({"state": "listening", "settings": {"speed": None, "speaker_id": "x"}})
    assert v.speed == 1.2 and v.speaker_id is None
