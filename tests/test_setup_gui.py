"""The web setup: the session's steps with fakes, the pure request handler, and the real server
on port 0 through http.client. No browser, no network, nothing installed."""

import asyncio
import http.client
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

import numpy as np
import pytest

from zundamon_jarvis import setup_gui, setup_wizard
from zundamon_jarvis.config import load_config
from zundamon_jarvis.mics import MicSource
from zundamon_jarvis.setup_gui import App, Request, SetupSession, handle

SPEAKERS = [
    {"name": "四国めたん", "styles": [{"name": "ノーマル", "id": 2}]},
    {"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3}]},
    {"name": "春日部つむぎ", "styles": [{"name": "ノーマル", "id": 8}]},
    {"name": "青山龍星", "styles": [{"name": "ノーマル", "id": 13}]},
]


def wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


class FakeVoicevox:
    def __init__(self):
        self.synthesized = []

    async def speakers(self):
        return SPEAKERS

    async def synth(self, text, speaker_id=None, *, speed=None):
        self.synthesized.append((speaker_id, speed))
        return np.zeros(10, np.float32)


class FakeAudio:
    def __init__(self, world, device=None):
        self.world = world
        self.device = device
        self.closed = False
        self.fallback = False
        world.audios.append(self)

    def open(self):
        if self.world.mic_fails:
            raise OSError("no mic")

    async def frames(self):
        for level in self.world.levels:
            yield np.full(512, level, np.float32)
        while not self.closed:
            await asyncio.sleep(0.01)

    def close(self):
        self.closed = True


class World:
    """Fakes for every Deps entry, recording what was done."""

    def __init__(self):
        self.login = "ok"
        self.engine_there = False
        self.model_there = False
        self.engine_fails = 0
        self.engine_gate = threading.Event()
        self.engine_gate.set()
        self.fetched = []
        self.vv = FakeVoicevox()
        self.engine_entered = 0
        self.engine_exited = 0
        self.played = []
        self.started = 0
        self.start_error = None
        self.running = False  # a daemon answers the control socket
        self.daemon_state = "listening"
        self.requests = []
        self.debug = {"paused": False, "frames_last_5s": 156, "max_abs_last_5s": 0.05}
        self.audios = []
        self.levels = [0.0, 0.25, 0.1]
        self.mic_fails = False
        self.picked = "/picked/folder"

    def install_engine(self, progress):
        self.engine_gate.wait(5)
        if self.engine_fails:
            self.engine_fails -= 1
            raise OSError("network down")
        progress("download", 50, 100)
        progress("download", 100, 100)
        progress("extract", 0, 0)
        progress("check", 0, 0)
        self.fetched.append("engine")
        self.engine_there = True

    def download_model(self, progress):
        progress(10, None)
        progress(40, 80)
        progress(80, 80)
        self.fetched.append("model")
        self.model_there = True

    async def login_check(self, model):
        return self.login

    @asynccontextmanager
    async def voice_engine(self, say):
        self.engine_entered += 1
        say("VOICEVOX を起動しています（少しかかります）…")
        try:
            yield self.vv
        finally:
            self.engine_exited += 1

    async def play(self, pcm):
        self.played.append(pcm)

    def start_services(self):
        if self.start_error:
            raise self.start_error
        self.started += 1
        self.running = True

    def request(self, cmd, timeout=3.0):
        self.requests.append(cmd)
        if not self.running:
            raise FileNotFoundError("no daemon")
        if cmd == "pause":
            self.daemon_state = "paused"
        status = {"state": self.daemon_state, "text": "Listening"}
        return {**status, "debug": dict(self.debug)} if cmd == "debug" else status

    def deps(self, picker=True):
        async def no_sleep(_s):
            await asyncio.sleep(0)

        return setup_gui.Deps(
            login_check=self.login_check, voice_engine=self.voice_engine, play=self.play,
            start_services=self.start_services, request=self.request,
            audio_factory=lambda device=None: FakeAudio(self, device),
            list_mics=lambda: [MicSource("usb-mic", "USB Mic"), MicSource("builtin", "Built-in")],
            default_mic=lambda: "builtin",
            engine_installed=lambda: self.engine_there, install_engine=self.install_engine,
            model_installed=lambda: self.model_there, download_model=self.download_model,
            pick_folder=(lambda: self.picked) if picker else None, sleep=no_sleep)


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def world():
    return World()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path, world):
    made = []

    def _make(system="mac", picker=True, config_text=None, clock=None):
        path = tmp_path / "config.toml"
        if config_text is not None:
            path.write_text(config_text)
        cfg, problem = setup_wizard.read_config(path)
        assert problem is None
        s = SetupSession(world.deps(picker), cfg, path, system, **({"clock": clock} if clock else {}))
        made.append(s)
        s.open()
        return s

    yield _make
    for s in made:
        s.close()


def done_downloading(s):
    wait_until(lambda: s.engine.state == "done" and s.model.state == "done")


# ---- the steps ----

def test_downloads_fetch_what_is_missing_with_progress(make, world):
    world.engine_gate.clear()
    s = make()
    wait_until(lambda: s.engine.state == "running")
    snap = s.snapshot()["prereq"]
    assert snap["engine"]["state"] == "running" and snap["model"]["state"] == "waiting"
    assert s.downloading
    world.engine_gate.set()
    done_downloading(s)
    assert world.fetched == ["engine", "model"]
    snap = s.snapshot()["prereq"]
    assert (snap["engine"]["done"], snap["engine"]["total"]) == (100, 100)
    assert (snap["model"]["done"], snap["model"]["total"]) == (80, 80)
    assert not s.downloading


def test_downloads_skip_what_is_there(make, world):
    world.engine_there = world.model_there = True
    s = make()
    done_downloading(s)
    assert world.fetched == []


def test_a_failed_download_can_be_retried(make, world):
    world.engine_fails = 1
    s = make()
    wait_until(lambda: s.engine.state == "error" and s.model.state == "done")
    assert "network down" in s.snapshot()["prereq"]["engine"]["error"]
    s.ensure_prereqs()
    done_downloading(s)
    assert world.fetched == ["model", "engine"]


@pytest.mark.parametrize("result, note", [
    ("ok", None), ("limit", setup_wizard.LIMIT_NOTE), ("login", setup_gui.LOGIN_HELP)])
def test_login_check(make, world, result, note):
    world.login = result
    s = make()
    s.check_login()
    wait_until(lambda: s.login == result)
    assert s.snapshot()["login"] == {"state": result, "note": note}


def test_mic_meter_lists_choices_and_saves_the_pick(make, world, tmp_path):
    s = make()
    s.mic_start()
    wait_until(lambda: s.snapshot()["mic"]["status"] == "ok")
    mic = s.snapshot()["mic"]
    assert [c["label"] for c in mic["choices"]] == ["OS の既定のマイク（Built-in）", "USB Mic", "Built-in"]
    assert mic["choices"][0]["selected"] and mic["peak"] == 0.25 and mic["note"] is None
    assert world.audios[0].device is None
    s.mic_start(1)
    wait_until(lambda: len(world.audios) == 2 and world.audios[0].closed)
    assert world.audios[1].device == "usb-mic"
    assert load_config(tmp_path / "config.toml").mic_device == "usb-mic"
    assert s.snapshot()["mic"]["choices"][1]["selected"]
    s.mic_start(0)  # back to the OS default: the key goes away
    assert "mic_device" not in (tmp_path / "config.toml").read_text()
    s.mic_stop()
    assert world.audios[-1].closed and s.snapshot()["mic"]["status"] == "idle"
    with pytest.raises(ValueError):
        s.mic_start(9)


def test_mic_silence_and_failure_explain_per_os(make, world):
    clock = Clock()
    world.levels = [0.0]
    s = make(system="mac", clock=clock)
    s.mic_start()
    wait_until(lambda: s.snapshot()["mic"]["status"] == "listening")
    clock.now += setup_gui.SILENT_AFTER_S
    mic = s.snapshot()["mic"]
    assert mic["status"] == "silent" and mic["note"] == setup_wizard.TERMINAL_MIC_NOTE
    world.mic_fails = True
    s.mic_start()
    wait_until(lambda: s.snapshot()["mic"]["status"] == "error")
    assert s.snapshot()["mic"]["note"] == setup_wizard.TERMINAL_MIC_NOTE


def test_voice_waits_for_the_engine_then_previews_and_saves(make, world, tmp_path):
    world.engine_gate.clear()
    s = make()
    s.voice_open()
    time.sleep(0.1)
    assert s.voice.state == "waiting" and world.engine_entered == 0
    world.engine_gate.set()
    wait_until(lambda: s.voice.state == "ready")
    v = s.snapshot()["voice"]
    assert [c["label"] for c in v["choices"]] == [f"{n} {st}" for n, st in setup_wizard.VOICES]
    assert v["current_label"] == "青山龍星 ノーマル（既定）"
    s.set_speed(1.6)
    s.voice_preview(3)
    wait_until(lambda: s.voice.previewing is None and world.played)
    assert world.vv.synthesized == [(3, 1.6)]
    s.voice_select(8)
    cfg = load_config(tmp_path / "config.toml")
    assert (cfg.speaker_id, cfg.speed) == (8, 1.6)
    assert s.snapshot()["voice"]["current_label"] == "春日部つむぎ ノーマル"
    with pytest.raises(ValueError):
        s.voice_select(999)
    with pytest.raises(ValueError):
        s.set_speed(3.0)


def test_voice_down_when_no_engine_answers(make, world):
    @asynccontextmanager
    async def none(say):
        yield None

    s = make()
    s.deps.voice_engine = none
    s.voice_open()
    wait_until(lambda: s.voice.state == "down")
    with pytest.raises(setup_gui.Refused):
        s.voice_preview(3)


def test_knowledge_folder(make, world, home, tmp_path):
    (home / "notes").mkdir()
    s = make()
    with pytest.raises(setup_gui.Refused, match="見つかりません"):
        s.set_knowledge("~/nope")
    s.set_knowledge(" ~/notes ")
    assert 'knowledge_dir = "~/notes"' in (tmp_path / "config.toml").read_text()
    assert s.snapshot()["knowledge"] == {"value": "~/notes", "can_pick": True}
    s.set_knowledge("")
    assert "knowledge_dir" not in (tmp_path / "config.toml").read_text()
    assert s.pick_folder() == "/picked/folder"


def test_no_folder_dialog_without_tkinter(make):
    s = make(picker=False)
    assert s.snapshot()["knowledge"]["can_pick"] is False
    with pytest.raises(setup_gui.Refused):
        s.pick_folder()


def test_model(make, tmp_path):
    s = make()
    s.set_model("haiku")
    assert load_config(tmp_path / "config.toml").model == "haiku"
    with pytest.raises(ValueError):
        s.set_model("gpt")


def test_start_needs_login_and_downloads(make, world):
    world.engine_gate.clear()
    s = make()
    with pytest.raises(setup_gui.Refused, match="ログイン"):
        s.start_services()
    s.check_login()
    wait_until(lambda: s.login == "ok")
    with pytest.raises(setup_gui.Refused, match="ダウンロード|準備"):
        s.start_services()
    world.engine_gate.set()
    done_downloading(s)
    s.start_services()
    wait_until(lambda: s.start.state == "done")
    assert world.started == 1


def ready(s):
    s.check_login()
    wait_until(lambda: s.login == "ok")
    done_downloading(s)


def test_start_on_mac_checks_the_daemon_mic(make, world):
    s = make(system="mac")
    ready(s)
    s.mic_start()
    s.voice_open()
    wait_until(lambda: s.voice.state == "ready")
    s.start_services()
    wait_until(lambda: s.start.state == "done")
    st = s.snapshot()["start"]
    assert st["status_text"] == "Listening" and st["daemon_mic"] == "ok"
    assert st["daemon_mic_text"] == setup_wizard.DAEMON_MIC_OK
    assert world.audios[0].closed and world.engine_exited == 1  # released before the daemon starts
    assert s.started


def test_start_daemon_mic_silent_then_fixed(make, world):
    world.debug["max_abs_last_5s"] = 0.0
    s = make(system="mac")
    ready(s)
    s.start_services()
    wait_until(lambda: s.start.state == "done")
    assert s.snapshot()["start"]["daemon_mic_text"] == setup_wizard.DAEMON_MIC_NOTE
    world.debug["max_abs_last_5s"] = 0.02
    s.recheck_daemon_mic()
    wait_until(lambda: s.start.daemon_mic == "ok")


def test_start_on_linux_skips_the_daemon_mic_check(make, world):
    s = make(system="linux")
    ready(s)
    s.start_services()
    wait_until(lambda: s.start.state == "done")
    assert s.snapshot()["start"]["daemon_mic"] is None and "debug" not in world.requests
    with pytest.raises(setup_gui.Refused):
        s.recheck_daemon_mic()


def test_start_failure_is_shown_and_can_be_retried(make, world):
    world.start_error = RuntimeError("zundamon-jarvis executable not found")
    s = make()
    ready(s)
    s.start_services()
    wait_until(lambda: s.start.state == "error")
    assert "常駐の設定ができませんでした" in s.snapshot()["start"]["message"]
    assert not s.started
    world.start_error = None
    s.start_services()
    wait_until(lambda: s.start.state == "done")


def test_a_running_daemon_is_paused_and_resumed_when_setup_stops_early(make, world):
    world.running = True
    s = make()
    assert world.requests[:2] == ["status", "pause"] and s.paused
    s.close()
    assert world.requests[-1] == "resume"


def test_a_running_daemon_is_not_resumed_after_the_restart(make, world):
    world.running = True
    s = make()
    ready(s)
    s.start_services()
    wait_until(lambda: s.start.state == "done")
    s.close()
    assert "resume" not in world.requests


def test_a_daemon_left_paused_stays_paused(make, world):
    world.running, world.daemon_state = True, "paused"
    s = make()
    s.close()
    assert world.requests == ["status"]


def test_rerun_shows_current_values(make, home):
    (home / "kb").mkdir()
    s = make(config_text='speaker_id = 8\nknowledge_dir = "~/kb"\nmodel = "haiku"\nspeed = 1.5\n'
                         'mic_device = "usb-mic"\n')
    snap = s.snapshot()
    assert snap["knowledge"]["value"] == "~/kb" and snap["model"]["current"] == "haiku"
    assert snap["voice"]["current"] == 8 and snap["voice"]["speed"] == 1.5
    s.mic_start()
    assert next(c for c in s.snapshot()["mic"]["choices"] if c["selected"])["label"] == "USB Mic"


# ---- the request handler ----

TOKEN = "s3cret-token"


@pytest.fixture
def app(make):
    a = App(make(), TOKEN, port=4321)
    return a


def req(method, target, *, body=None, headers=None, cookie=True, ctype="application/json"):
    h = {"host": "127.0.0.1:4321"}
    if cookie:
        h["cookie"] = f"zj_setup_4321={TOKEN}"
    if body is not None:
        h["content-type"] = ctype
    h.update(headers or {})
    raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
    return Request(method, target, h, raw)


def test_the_token_is_traded_for_a_cookie(app):
    r = handle(app, req("GET", f"/?t={TOKEN}", cookie=False))
    assert r.status == 303 and ("Location", "/") in r.headers
    cookie = dict(r.headers)["Set-Cookie"]
    assert cookie.startswith(f"zj_setup_4321={TOKEN};") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert handle(app, req("GET", "/")).status == 200


@pytest.mark.parametrize("request_, status", [
    (req("GET", "/?t=wrong", cookie=False), 403),
    (req("GET", "/", cookie=False), 403),
    (req("GET", "/api/state", cookie=False), 403),
    (req("GET", "/api/state", headers={"cookie": "zj_setup_4321=wrong"}), 403),
    (req("GET", "/api/state", headers={"cookie": f"zj_setup_9999={TOKEN}"}), 403),  # another run's
    (req("GET", "/api/state", headers={"host": "evil.example:4321"}), 403),  # DNS rebinding
    (req("GET", "/api/state", headers={"host": "127.0.0.1:80"}), 403),
    (req("POST", "/api/model", body={"model": "opus"}, headers={"origin": "http://evil.example"}), 403),
    (req("POST", "/api/model", body=b"model=opus", ctype="application/x-www-form-urlencoded"), 415),
    (req("POST", "/api/model", body=b"model=opus", ctype="text/plain"), 415),
    (req("POST", "/api/model", body=b"[1]"), 400),
    (req("POST", "/api/model", body=b"{nope"), 400),
    (req("POST", "/api/nope", body={}), 404),
    (req("GET", "/assets/../setup_gui.py"), 404),
    (req("GET", "/assets/index.html"), 404),
    (req("DELETE", "/api/state"), 405),
])
def test_refusals(app, request_, status):
    assert handle(app, request_).status == status


def test_refusals_change_nothing(app, tmp_path):
    handle(app, req("POST", "/api/model", body={"model": "opus"}, headers={"host": "evil:4321"}))
    handle(app, req("POST", "/api/model", body=b'{"model": "opus"}', ctype="text/plain"))
    assert not (tmp_path / "config.toml").exists()


def test_localhost_and_same_origin_are_fine(app):
    assert handle(app, req("GET", "/api/state", headers={"host": "localhost:4321"})).status == 200
    r = handle(app, req("POST", "/api/model", body={"model": "opus"},
                        headers={"origin": "http://127.0.0.1:4321"}))
    assert r.status == 200 and json.loads(r.body)["state"]["model"]["current"] == "opus"


def test_errors_come_back_as_json(app):
    r = handle(app, req("POST", "/api/knowledge", body={"path": "~/missing"}))
    assert r.status == 409 and "見つかりません" in json.loads(r.body)["error"]
    r = handle(app, req("POST", "/api/voice/preview", body={"speaker_id": "3"}))
    assert r.status == 400
    r = handle(app, req("POST", "/api/speed", body={"speed": True}))
    assert r.status == 400


def test_static_files(app):
    for path, ctype in (("/", "text/html"), ("/assets/setup.js", "text/javascript"),
                        ("/assets/setup.css", "text/css"), ("/assets/icon.png", "image/png")):
        r = handle(app, req("GET", path))
        assert r.status == 200 and r.content_type.startswith(ctype) and r.body


def test_heartbeat_and_goodbye(app):
    clock = Clock()
    app.clock = clock
    assert app.idle_for() is None  # the page never came
    handle(app, req("GET", "/api/state"))
    assert app.idle_for() == -setup_gui.GONE_S
    clock.now += setup_gui.GONE_S + 1
    assert app.idle_for() > 0
    handle(app, req("GET", "/api/state"))
    handle(app, req("POST", "/api/bye", body={}))
    clock.now += setup_gui.BYE_S + 1
    assert app.idle_for() > 0
    handle(app, req("GET", "/api/state"))  # a reload came back
    assert app.idle_for() < 0


def test_finish_sets_the_flag(app):
    assert handle(app, req("POST", "/api/finish", body={})).status == 200
    assert app.session.finished.is_set()


# ---- the real server and run_gui ----

def test_over_http(make):
    app = App(make(), TOKEN)
    server = setup_gui.make_server(app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
        c.request("GET", f"/?t={TOKEN}")
        r = c.getresponse()
        r.read()
        assert r.status == 303
        cookie = r.getheader("Set-Cookie").split(";")[0]
        c.request("POST", "/api/model", body=json.dumps({"model": "opus"}),
                  headers={"Content-Type": "application/json", "Cookie": cookie})
        r = c.getresponse()
        data = json.loads(r.read())
        assert r.status == 200 and data["state"]["model"]["current"] == "opus"
        assert "default-src 'none'" in r.getheader("Content-Security-Policy")
        assert r.getheader("Cache-Control") == "no-store"
        c.request("GET", "/api/state", headers={"Cookie": cookie, "Host": "attacker.example"})
        r = c.getresponse()
        r.read()
        assert r.status == 403
        c.request("POST", "/api/model", body="x" * (setup_gui.MAX_BODY + 1),
                  headers={"Content-Type": "application/json", "Cookie": cookie})
        r = c.getresponse()
        r.read()
        assert r.status == 413
    finally:
        server.shutdown()
        server.server_close()


def test_run_gui_serves_until_finished(tmp_path, world):
    lines = []
    opened = []

    def browser(url):
        opened.append(url)

        def visit():
            u = urllib.parse.urlsplit(url)
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
            c.request("GET", f"{u.path}?{u.query}")
            r = c.getresponse()
            r.read()
            cookie = r.getheader("Set-Cookie").split(";")[0]
            c.request("POST", "/api/finish", body="{}",
                      headers={"Content-Type": "application/json", "Cookie": cookie})
            c.getresponse().read()

        threading.Thread(target=visit, daemon=True).start()
        return True

    code = setup_gui.run_gui(world.deps(), config_path=tmp_path / "config.toml", system="linux",
                             open_browser=browser, out=lines.append)
    assert code == 1  # finished without starting the services
    assert opened and opened[0].startswith("http://127.0.0.1:") and "?t=" in opened[0]
    assert any(opened[0] in line for line in lines)
    assert "続きから" in lines[-1]


def test_run_gui_reports_a_broken_config(tmp_path, world):
    (tmp_path / "config.toml").write_text("speed = = 1\n")
    lines = []
    assert setup_gui.run_gui(world.deps(), config_path=tmp_path / "config.toml", system="linux",
                             open_browser=lambda url: pytest.fail("no page for a broken config"),
                             out=lines.append) == 1
    assert "読めません" in lines[0]


def test_no_console_browser_without_a_display():
    assert setup_gui._open_browser("linux", {})("http://127.0.0.1:1/") is False
    import webbrowser

    assert setup_gui._open_browser("linux", {"WAYLAND_DISPLAY": "wayland-1"}) is webbrowser.open
    assert setup_gui._open_browser("mac", {}) is webbrowser.open


def test_answers_saved_at_once_do_not_lose_each_other(make, tmp_path):
    s = make()
    threads = [threading.Thread(target=s.set_speed, args=(1.0 + i / 10,)) for i in range(5)]
    threads += [threading.Thread(target=s.set_model, args=("opus",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.model == "opus" and cfg.speed == s.cfg.speed


def test_main_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_gui, "run_gui", lambda: seen.append("gui") or 0)
    monkeypatch.setattr(setup_wizard, "main", lambda argv: seen.append(("cli", argv)) or 0)
    assert setup_gui.main([]) == 0 and setup_gui.main(["--cli"]) == 0
    assert seen == ["gui", ("cli", [])]
    assert setup_gui.main(["--what"]) == 2


# ---- the page itself ----

def _asset(name):
    return resources.files("zundamon_jarvis").joinpath("assets", "setup", name).read_text(encoding="utf-8")


def test_the_page_loads_nothing_from_outside():
    html = _asset("index.html")
    for text in (html, _asset("setup.css"), _asset("setup.js")):
        assert "http://" not in text and "https://" not in text and "//cdn" not in text
    assert 'src="/assets/setup.js"' in html and 'href="/assets/setup.css"' in html
    assert "<script>" not in html and "style=" not in html  # the CSP allows no inline code


def test_the_page_script_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    path = Path(str(resources.files("zundamon_jarvis").joinpath("assets", "setup", "setup.js")))
    r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_page_calls_only_routes_that_exist():
    import re

    called = set(re.findall(r'"(/api/[a-z/-]+)"', _asset("setup.js")))
    assert called - {"/api/state"} <= set(setup_gui.ROUTES)


def test_demo_deps_build_for_every_os():
    from zundamon_jarvis import setup_demo

    for system in ("mac", "windows", "linux"):
        deps = setup_demo.demo_deps(system)
        assert deps.engine_installed() is False and deps.model_installed() is False


# ---- the app window ----

def _visit_and_finish(url):
    u = urllib.parse.urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    c.request("GET", f"{u.path}?{u.query}")
    r = c.getresponse()
    r.read()
    cookie = r.getheader("Set-Cookie").split(";")[0]
    c.request("POST", "/api/finish", body="{}", headers={"Content-Type": "application/json", "Cookie": cookie})
    c.getresponse().read()


class FakeWindow:
    """Stands in for AppWindow: run() blocks like webview.start() until close()."""

    def __init__(self, page=None, fail=False):
        self.page = page
        self.fail = fail
        self.closed = threading.Event()
        self.urls = []

    def run(self, url):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("no WebView2")
        if self.page:
            threading.Thread(target=self.page, args=(url,), daemon=True).start()
        assert self.closed.wait(10), "the window was never closed"

    def close(self):
        self.closed.set()

    def pick_folder(self):
        return "/picked"


def test_run_gui_in_a_window_closes_it_when_setup_finishes(tmp_path, world):
    lines = []
    win = FakeWindow(page=_visit_and_finish)
    code = setup_gui.run_gui(world.deps(), config_path=tmp_path / "config.toml", system="windows",
                             open_browser=lambda url: pytest.fail("no browser with a window"),
                             out=lines.append, window=win)
    assert code == 1  # finished without starting the services
    assert win.closed.is_set() and win.urls[0].startswith("http://127.0.0.1:")
    assert not any("?t=" in line for line in lines)  # nothing to paste: the window has it


def test_closing_the_window_ends_setup(tmp_path, world):
    win = FakeWindow()
    threading.Timer(0.3, win.close).start()  # the user closes it
    code = setup_gui.run_gui(world.deps(), config_path=tmp_path / "config.toml", system="mac",
                             open_browser=lambda url: pytest.fail("no browser with a window"),
                             out=lambda *a: None, window=win)
    assert code == 1


def test_the_window_supplies_the_folder_dialog(tmp_path, world):
    deps = world.deps()
    win = FakeWindow()
    threading.Timer(0.3, win.close).start()
    setup_gui.run_gui(deps, config_path=tmp_path / "config.toml", system="mac", out=lambda *a: None,
                      window=win)
    assert deps.pick_folder() == "/picked"


def test_no_window_falls_back_to_the_browser(tmp_path, world):
    lines = []
    opened = []
    win = FakeWindow(fail=True)

    def browser(url):
        opened.append(url)
        threading.Thread(target=_visit_and_finish, args=(url,), daemon=True).start()
        return True

    setup_gui.run_gui(world.deps(), config_path=tmp_path / "config.toml", system="windows",
                      open_browser=browser, out=lines.append, window=win)
    assert opened and any(opened[0] in line for line in lines)


def test_app_window_only_on_mac_and_windows(monkeypatch):
    assert setup_gui.app_window("linux") is None

    class FakeWebview:
        pass

    monkeypatch.setitem(sys.modules, "webview", FakeWebview)
    assert isinstance(setup_gui.app_window("windows"), setup_gui.AppWindow)
    monkeypatch.setitem(sys.modules, "webview", None)  # not installed
    assert setup_gui.app_window("mac") is None


def test_app_window_drives_pywebview():
    calls = []

    class Win:
        def destroy(self):
            calls.append("destroy")

        def create_file_dialog(self, kind, directory=""):
            calls.append(("dialog", kind))
            return ("/Users/me/notes",)

    class Webview:
        class FileDialog:
            FOLDER = "folder"

        @staticmethod
        def create_window(title, url, **kw):
            calls.append(("create", title, url, kw["width"], kw["height"]))
            return Win()

        @staticmethod
        def start():
            calls.append("start")

    w = setup_gui.AppWindow(Webview)
    assert w.pick_folder() is None  # no window yet
    w.run("http://127.0.0.1:1/?t=x")
    assert w.pick_folder() == "/Users/me/notes"
    w.close()
    assert calls == [("create", setup_gui.WINDOW_TITLE, "http://127.0.0.1:1/?t=x", 960, 720), "start",
                     ("dialog", "folder"), "destroy"]
