import sys
import asyncio
import tomllib
from contextlib import asynccontextmanager

import numpy as np
import pytest

from zundamon_jarvis import setup_wizard
from zundamon_jarvis.brain import AuthProblem, TextDelta, TurnEnd, UsageLimit
from zundamon_jarvis.config import load_config


class FakeConsole:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt!r}")
        return self.answers.pop(0)

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.prompts + self.lines)


SPEAKERS = [
    {"name": "四国めたん", "styles": [{"name": "ノーマル", "id": 2}]},
    {"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3}, {"name": "あまあま", "id": 1}]},
    {"name": "春日部つむぎ", "styles": [{"name": "ノーマル", "id": 8}]},
    {"name": "青山龍星", "styles": [{"name": "ノーマル", "id": 13}]},
]


class FakeVoicevox:
    def __init__(self) -> None:
        self.synthesized: list[int] = []

    async def speakers(self):
        return SPEAKERS

    async def synth(self, text, speaker_id=None, *, speed=None):
        self.synthesized.append(speaker_id)
        return np.zeros(10, np.float32)


class FakeServices:
    def __init__(self) -> None:
        self.started = 0
        self.requests: list[str] = []
        self.debug = {"paused": False, "frames_last_5s": 156, "max_abs_last_5s": 0.05}

    def start(self) -> None:
        self.started += 1

    def request(self, cmd: str, timeout: float = 3.0) -> dict:
        self.requests.append(cmd)
        if self.started:
            status = {"state": "listening", "text": "Listening"}
            return {**status, "debug": dict(self.debug)} if cmd == "debug" else status
        raise FileNotFoundError("no daemon")


def fakes(tmp_path, *, login="ok", peak=0.3, vv=None):
    services = FakeServices()
    played: list[np.ndarray] = []

    async def login_check(model):
        return login

    peaks = list(peak) if isinstance(peak, list) else None

    async def mic_peak(device):
        return peaks.pop(0) if peaks is not None else peak

    @asynccontextmanager
    async def voice_engine(say):
        yield vv if vv is not None else FakeVoicevox()

    async def play(pcm):
        played.append(pcm)

    async def no_sleep(_s):
        await asyncio.sleep(0)

    kw = dict(config_path=tmp_path / "config.toml", system="mac", login_check=login_check,
              mic_peak=mic_peak, voice_engine=voice_engine, play=play,
              start_services=services.start, request=services.request, sleep=no_sleep)
    return kw, services, played


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    return tmp_path


async def test_setup_stops_when_claude_login_fails(tmp_path):
    kw, services, _ = fakes(tmp_path, login="login")
    con = FakeConsole()
    assert await setup_wizard.run_setup(con, **kw) == 2
    assert "ターミナルで `claude` を実行してログインしてから、もう一度 `zundamon-jarvis setup` を" in con.text
    assert services.started == 0
    assert not (tmp_path / "config.toml").exists()


class ScriptedBrain:
    def __init__(self, events, hang=False):
        self._events = list(events)
        self.hang = hang
        self.submitted = []
        self.closed = False

    async def submit(self, text):
        self.submitted.append(text)

    async def events(self):
        for ev in self._events:
            yield ev
        if self.hang:
            await asyncio.Event().wait()

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("events, hang, ok", [
    ([TextDelta("OK"), TurnEnd(ok=True)], False, "ok"),
    ([AuthProblem(), TurnEnd(ok=False)], False, "login"),
    ([TurnEnd(ok=False)], False, "login"),
    ([UsageLimit(), TurnEnd(ok=False)], False, "limit"),
    ([], True, "login"),  # no reply within the timeout
])
async def test_claude_login_check(events, hang, ok):
    brains = []

    def factory(cwd, prompt_path, model):
        assert prompt_path.is_file() and model == "haiku"
        brains.append(ScriptedBrain(events, hang))
        return brains[-1]

    assert await setup_wizard.claude_login_check("haiku", brain_factory=factory, timeout=0.05) == ok
    assert brains[0].submitted == ["OKとだけ返して"] and brains[0].closed


async def test_claude_login_check_survives_a_crashing_brain():
    class Broken(ScriptedBrain):
        async def submit(self, text):
            raise RuntimeError("claude CLI not found")

    brain = Broken([])
    assert await setup_wizard.claude_login_check(None, brain_factory=lambda *a: brain) == "login"
    assert brain.closed


async def test_setup_first_run_saves_each_step(tmp_path, home):
    notes = home / "notes"
    notes.mkdir()
    vv = FakeVoicevox()
    kw, services, played = fakes(tmp_path, vv=vv)
    # voice: try 1 (ずんだもん), keep it; knowledge dir; model
    con = FakeConsole("1", "y", "~/notes", "opus")
    assert await setup_wizard.run_setup(con, **kw) == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.speaker_id == 3 and cfg.knowledge_dir == notes and cfg.model == "opus"
    assert tomllib.loads((tmp_path / "config.toml").read_text())["knowledge_dir"] == "~/notes"
    assert vv.synthesized == [3] and len(played) == 1
    assert services.started == 1
    assert "ヘッドホン" in con.text and "Listening" in con.text


async def test_setup_rerun_uses_current_values(tmp_path, home):
    notes = home / "notes"
    notes.mkdir()
    (tmp_path / "config.toml").write_text(
        '# mine\nspeaker_id = 8\nknowledge_dir = "~/notes"\nmodel = "haiku"\nspeed = 1.5\n')
    kw, services, played = fakes(tmp_path)
    con = FakeConsole("", "", "")  # Enter everywhere: keep what is there
    assert await setup_wizard.run_setup(con, **kw) == 0
    cfg = load_config(tmp_path / "config.toml")
    assert (cfg.speaker_id, cfg.knowledge_dir, cfg.model, cfg.speed) == (8, notes, "haiku", 1.5)
    assert (tmp_path / "config.toml").read_text().startswith("# mine\n")
    voice_q, knowledge_q, model_q = con.prompts
    assert "春日部つむぎ ノーマル" in con.text  # the current voice is shown by name
    assert "~/notes" in knowledge_q and "haiku" in model_q
    assert played == [] and services.started == 1


async def test_setup_knowledge_dir_rejects_missing_path(tmp_path, home):
    real = home / "kb"
    real.mkdir()
    kw, _, _ = fakes(tmp_path)
    con = FakeConsole("", str(home / "nope"), str(real), "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "見つかりません" in con.text
    assert con.prompts.count(con.prompts[1]) == 2  # asked again
    assert load_config(tmp_path / "config.toml").knowledge_dir == real


async def test_setup_model_rejects_unknown_and_defaults_to_sonnet(tmp_path):
    kw, _, _ = fakes(tmp_path)
    con = FakeConsole("", "", "gpt", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "sonnet" in con.prompts[2]
    assert load_config(tmp_path / "config.toml").model == "sonnet"


async def test_setup_explains_mic_permission_on_silence(tmp_path):
    kw, services, _ = fakes(tmp_path, peak=0.0)
    con = FakeConsole("", "", "", "")  # Enter after "許可", then voice / knowledge / model
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "許可を押したら Enter" in con.prompts[0]
    assert setup_wizard.TERMINAL_MIC_NOTE in con.text and "音が取れませんでした" in con.text
    assert services.started == 1  # carries on


async def test_setup_probes_the_mic_again_after_the_permission_dialog(tmp_path):
    kw, _, _ = fakes(tmp_path, peak=[0.0, 0.2])
    con = FakeConsole("", "", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "音が取れました" in con.text and setup_wizard.TERMINAL_MIC_NOTE not in con.text


async def test_setup_linux_does_not_ask_again_on_silence(tmp_path):
    kw, _, _ = fakes(tmp_path, peak=0.0)
    kw["system"] = "linux"
    con = FakeConsole("", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "ミュート" in con.text


async def test_setup_says_usage_limit_is_not_a_login_problem(tmp_path):
    kw, services, _ = fakes(tmp_path, login="limit")
    con = FakeConsole("", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert setup_wizard.LIMIT_NOTE in con.text and setup_wizard.LOGIN_HELP not in con.text
    assert services.started == 1


async def test_setup_checks_the_daemon_mic_and_explains_python_permission(tmp_path):
    kw, services, _ = fakes(tmp_path)
    services.debug = {"paused": False, "frames_last_5s": 156, "max_abs_last_5s": 0.0}
    con = FakeConsole("", "", "", "")  # voice, knowledge, model, Enter to check again
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert con.text.count(setup_wizard.DAEMON_MIC_NOTE) == 2  # checked twice, still silent
    assert "python3.12" in setup_wizard.DAEMON_MIC_NOTE and "tccutil reset Microphone" in setup_wizard.DAEMON_MIC_NOTE
    assert "launchctl kickstart -k gui/$UID/dev.zundamon-jarvis.daemon" in setup_wizard.DAEMON_MIC_NOTE
    assert services.requests.count("debug") == 2 * setup_wizard.DAEMON_MIC_CHECK_S


async def test_setup_daemon_mic_ok(tmp_path):
    kw, services, _ = fakes(tmp_path)
    con = FakeConsole("", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "常駐しているほうにも音が届いています" in con.text
    assert setup_wizard.DAEMON_MIC_NOTE not in con.text


async def test_setup_reports_a_broken_config_without_a_traceback(tmp_path):
    (tmp_path / "config.toml").write_text("speed = = 1\n")
    kw, services, _ = fakes(tmp_path)
    con = FakeConsole()
    assert await setup_wizard.run_setup(con, **kw) == 1
    assert "読めません" in con.text and services.started == 0


async def test_setup_reports_a_missing_executable(tmp_path):
    kw, services, _ = fakes(tmp_path)

    def no_exe():
        raise RuntimeError("zundamon-jarvis executable not found; install it first (uv tool install .)")

    kw["start_services"] = no_exe
    con = FakeConsole("", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 1
    assert "常駐の設定ができませんでした" in con.text


@pytest.mark.parametrize("typed, want", [
    ("'/Users/taro/My Notes'", "/Users/taro/My Notes"),
    ('"/Users/taro/My Notes" ', "/Users/taro/My Notes"),
    ("/Users/taro/My\\ Notes\\ \\(old\\)", "/Users/taro/My Notes (old)"),
    ("~/notes", "~/notes"),
])
def test_clean_path_input(typed, want):
    assert setup_wizard.clean_path_input(typed, "mac") == want


@pytest.mark.skipif(sys.platform == "win32", reason="backslash-escaped drops are a macOS/Linux terminal habit")
async def test_setup_knowledge_dir_accepts_a_dragged_folder(tmp_path, home):
    real = home / "My Notes"
    real.mkdir()
    kw, _, _ = fakes(tmp_path)
    con = FakeConsole("", str(real).replace(" ", "\\ ") + " ", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert load_config(tmp_path / "config.toml").knowledge_dir == real


async def test_setup_skips_voice_when_engine_is_down(tmp_path):
    kw, _, _ = fakes(tmp_path)

    @asynccontextmanager
    async def no_engine(say):
        yield None

    kw["voice_engine"] = no_engine
    con = FakeConsole("", "")  # knowledge, model only
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "VOICEVOX" in con.text


async def test_setup_pauses_a_running_daemon_while_previewing(tmp_path):
    kw, services, _ = fakes(tmp_path)
    services.started = 1  # already running (a re-run)
    con = FakeConsole("", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert services.requests[:2] == ["status", "pause"]
    assert "resume" not in services.requests  # the restart at the end resumes it


async def test_setup_resumes_the_daemon_when_aborted(tmp_path):
    kw, services, _ = fakes(tmp_path)
    services.started = 1
    con = FakeConsole()  # the first question raises (stands in for Ctrl-C)
    with pytest.raises(AssertionError):
        await setup_wizard.run_setup(con, **kw)
    assert services.requests[-1] == "resume"


@pytest.mark.parametrize("answers, engine_kept, config_kept", [
    (("y", "y"), False, False),
    (("", "n"), True, True),
])
def test_uninstall_asks_about_engine_and_config(tmp_path, answers, engine_kept, config_kept):
    engine = tmp_path / "voicevox_engine"
    (engine / "lib").mkdir(parents=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text("speed = 1.2\n")
    removed = []
    con = FakeConsole(*answers)
    assert setup_wizard.run_uninstall(con, system="mac", uninstall=removed.append,
                                      engine_dir=engine, config_path=cfg) == 0
    assert removed == ["mac"]
    assert engine.exists() is engine_kept and cfg.exists() is config_kept
    assert "uv tool uninstall zundamon-jarvis" in con.text
    assert "models--mlx-community--whisper-large-v3-turbo" in con.text


def test_uninstall_skips_questions_for_missing_things(tmp_path):
    con = FakeConsole()
    assert setup_wizard.run_uninstall(con, system="linux", uninstall=lambda s: None,
                                      engine_dir=tmp_path / "none", config_path=tmp_path / "c.toml") == 0
    assert con.prompts == []


@pytest.mark.parametrize("system, needle", [
    ("mac", "Mac のスピーカー"),
    ("windows", "Windows にはエコーキャンセルがない"),
    ("linux", "zundamon_jarvis_aec_source"),
])
def test_headphones_note_is_per_os(system, needle):
    assert needle in setup_wizard.headphones_note(system)


def test_mic_problem_note_per_os():
    assert setup_wizard.mic_problem_note("mac", "open") == setup_wizard.TERMINAL_MIC_NOTE
    assert "デスクトップ アプリ" in setup_wizard.mic_problem_note("windows", "silent")
    assert "ミュート" in setup_wizard.mic_problem_note("linux", "silent")
    assert "つながっているか" in setup_wizard.mic_problem_note("linux", "open")


async def test_setup_windows_checks_the_daemon_mic_with_windows_advice(tmp_path):
    kw, services, _ = fakes(tmp_path)
    kw["system"] = "windows"
    services.debug = {"paused": False, "frames_last_5s": 156, "max_abs_last_5s": 0.0}
    con = FakeConsole("", "", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert con.text.count(setup_wizard.WINDOWS_DAEMON_MIC_NOTE) == 2
    assert "doctor | clip" in setup_wizard.WINDOWS_DAEMON_MIC_NOTE
    assert "python3.12" not in con.text and "pbcopy" not in con.text


async def test_setup_linux_skips_the_daemon_mic_check(tmp_path):
    kw, services, _ = fakes(tmp_path)
    kw["system"] = "linux"
    con = FakeConsole("", "", "")
    assert await setup_wizard.run_setup(con, **kw) == 0
    assert "debug" not in services.requests


def test_engine_command_uses_the_gpu_on_windows(tmp_path):
    run = tmp_path / "run.exe"
    assert setup_wizard.engine_command(run, "windows")[-1] == "--use_gpu"
    assert "--use_gpu" not in setup_wizard.engine_command(tmp_path / "run", "linux")
    assert "creationflags" in setup_wizard._detached("windows")
    assert setup_wizard._detached("mac") == {"start_new_session": True}


def test_install_and_start_on_windows_only_installs(monkeypatch):
    calls = []
    monkeypatch.setattr("zundamon_jarvis.service.install_services", calls.append)
    monkeypatch.setattr(setup_wizard.subprocess, "run", lambda *a, **k: calls.append(("run", a)))
    setup_wizard.install_and_start("windows")
    assert calls == ["windows"]


def test_resolve_folder(tmp_path, home):
    (home / "notes").mkdir()
    assert setup_wizard.resolve_folder("~/notes") == home / "notes"
    assert setup_wizard.resolve_folder("notes", cwd=home) == home / "notes"
    assert setup_wizard.resolve_folder("'~/notes' ") == home / "notes"
    assert setup_wizard.resolve_folder("~/missing") is None
    assert setup_wizard.resolve_folder("  ") is None


def test_save_model_rejects_unknown(tmp_path):
    from zundamon_jarvis.config import Config

    with pytest.raises(ValueError):
        setup_wizard.save_model("gpt", Config(), tmp_path / "c.toml")
    assert not (tmp_path / "c.toml").exists()


def test_clean_path_input_keeps_windows_backslashes():
    assert setup_wizard.clean_path_input(r"C:\Users\taro\My Notes", "windows") == r"C:\Users\taro\My Notes"
    assert setup_wizard.clean_path_input(r'"C:\Users\taro\notes"', "windows") == r"C:\Users\taro\notes"
