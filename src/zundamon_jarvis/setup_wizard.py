"""`zundamon-jarvis setup --cli`: the first-run questions, in order, in the terminal.

(Plain `zundamon-jarvis setup` is the same steps as a local web page: ``zundamon_jarvis.setup_gui``.
Both use the step helpers here, so what is saved and what is said stays the same.)

1. Claude login (one short turn through ``ClaudeBrain``; no reply in 60 s -> exit 2)
2. Microphone (open it for 1 s: on a Mac this brings up the permission dialog)
3. Headphones advice
4. Voice (a few candidates, previewed by number)
5. ``knowledge_dir`` (optional)
6. Claude model
7. Install the services and start them

Every answer is written to ``config.toml`` as soon as it is given; a re-run offers the current
values (Enter keeps them). The engine and the speech model are not fetched here: the installer
does that before the terminal wizard (the web page fetches them itself). Everything outside (input/output, Claude, the mic, VOICEVOX,
playback, services, the control socket) is a parameter so tests use fakes.
"""

import asyncio
import contextlib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zundamon_jarvis import config as config_mod
from zundamon_jarvis import platform
from zundamon_jarvis.brain import AuthProblem, PermissionRequest, TurnEnd, UsageLimit
from zundamon_jarvis.config import MODELS, Config, load_config, update_config
from zundamon_jarvis.tts import DEFAULT_SPEAKER, DEFAULT_URL, RATE, talk_styles

log = logging.getLogger(__name__)

EXIT_LOGIN = 2
LOGIN_PROMPT = "OKとだけ返して"
LOGIN_TIMEOUT_S = 60.0
LOGIN_HELP = ("Claude にログインできていません。"
              "ターミナルで `claude` を実行してログインしてから、もう一度 `zundamon-jarvis setup` を実行してください")
LIMIT_NOTE = ("Claude にはログインできていますが、いまは利用上限に達しています。"
              "設定はこのまま続けられます。上限が戻れば（時間をおけば）答えるようになります。")
MIC_SECONDS = 1.0
# The probe runs as a child of the terminal app, so this grant is the terminal's.
TERMINAL_MIC_NOTE = ("システム設定 > プライバシーとセキュリティ > マイク で、"
                     "使っているターミナル（「ターミナル」など）をオンにしてください。")
# The LaunchAgent daemon runs uv's own python3.12, which macOS asks about (and lists) separately.
DAEMON_MIC_NOTE = """常駐しているほう（バックグラウンドの zundamon-jarvis）にマイクの音が届いていません。
システム設定 > プライバシーとセキュリティ > マイク で「python3.12」を探して、オンにしてください。
一覧にないときは、ターミナルで次の 2 行を実行してから、もう一度話しかけてください
（1 行目はほかのアプリのマイクの許可も取り消すので、次に使うときにまた聞かれます）:
  tccutil reset Microphone
  launchctl kickstart -k gui/$UID/dev.zundamon-jarvis.daemon
それでも音が届かないときは `zundamon-jarvis doctor | pbcopy` の結果を送ってください。"""
WINDOWS_DAEMON_MIC_NOTE = """常駐しているほう（バックグラウンドの zundamon-jarvis）にマイクの音が届いていません。
設定 > プライバシーとセキュリティ > マイク で「マイクへのアクセス」と
「デスクトップ アプリにマイクへのアクセスを許可する」をオンにしてから、もう一度話しかけてください。
それでも音が届かないときは `zundamon-jarvis doctor | clip` の結果を送ってください。"""
DAEMON_MIC_CHECK_S = 5
DAEMON_MIC_SYSTEMS = ("mac", "windows")  # where an app can be refused the mic: frames of zeros
WINDOWS_MIC_NOTE = ("設定 > プライバシーとセキュリティ > マイク で「マイクへのアクセス」と"
                    "「デスクトップ アプリにマイクへのアクセスを許可する」をオンにしてください。")
HEADPHONES_NOTE = (
    "ヘッドホン（イヤホン）での利用をおすすめします。Mac のスピーカーから声を出すと、"
    "マイクがその声を拾ってしまうので、話している間は聞き取りを止めます（途中で割り込めません）。"
    "ヘッドホンなら、しゃべっている途中でも話しかけて止められます。")
# Windows has no echo cancellation either; the daemon cannot tell its speakers apart from headphones.
WINDOWS_HEADPHONES_NOTE = (
    "ヘッドホン（イヤホン）での利用をおすすめします。Windows にはエコーキャンセルがないので、"
    "PC のスピーカーから声を出すと、マイクがずんだもんの声を拾ってしまいます。"
    "ヘッドホンなら、しゃべっている途中でも話しかけて止められます。")
LINUX_HEADPHONES_NOTE = (
    "ヘッドホン（イヤホン）での利用をおすすめします。スピーカーで使うときは、PipeWire のエコーキャンセル"
    "（リポジトリの deploy/install-audio.sh）を入れて、マイクに「zundamon_jarvis_aec_source」を選んでください。"
    "どちらでも、しゃべっている途中に話しかけて止められます。")
VOICES = [("ずんだもん", "ノーマル"), ("青山龍星", "ノーマル"), ("四国めたん", "ノーマル"),
          ("春日部つむぎ", "ノーマル")]
SAMPLE = "こんにちは。ご用件をどうぞ。"
ENGINE_WAIT_S = 120.0
STATUS_WAIT_S = 60  # the daemon opens its socket after loading the speech model
YES = ("", "y", "yes", "はい")


@dataclass
class Console:
    ask: Callable[[str], str] = input
    say: Callable[..., None] = print


# ---- 1. Claude login ----

BrainFactory = Callable[[Path, Path, str | None], Any]  # (cwd, prompt_path, model) -> Brain


def _claude_brain(cwd: Path, prompt_path: Path, model: str | None) -> Any:
    from zundamon_jarvis.brain import ClaudeBrain

    return ClaudeBrain(cwd, [], prompt_path=prompt_path, model=model)


LoginResult = str  # "ok" | "login" (not usable: no login, no CLI, no answer) | "limit"


async def claude_login_check(model: str | None, *, brain_factory: BrainFactory | None = None,
                             timeout: float = LOGIN_TIMEOUT_S) -> LoginResult:
    """One short turn: "ok" if Claude answers it, "limit" if the usage limit stopped it (the
    login itself works), "login" for anything else (rejected login, no CLI, no answer in time).

    It runs in an empty temporary directory, so no project's CLAUDE.md or hooks (conversation
    capture, ...) see this throwaway turn.
    """
    with tempfile.TemporaryDirectory(prefix="zundamon-jarvis-setup-") as tmp:
        prompt = Path(tmp) / "prompt.md"
        prompt.write_text("Answer in as few words as possible.\n", encoding="utf-8")
        brain = (brain_factory or _claude_brain)(Path(tmp), prompt, model)

        async def one_turn() -> LoginResult:
            limited = False
            await brain.submit(LOGIN_PROMPT)
            async for ev in brain.events():
                if isinstance(ev, AuthProblem):
                    return "login"
                if isinstance(ev, UsageLimit):
                    limited = True
                elif isinstance(ev, PermissionRequest):
                    ev.decide(False)
                elif isinstance(ev, TurnEnd):
                    return "limit" if limited else "ok" if ev.ok else "login"
            return "login"

        try:
            return await asyncio.wait_for(one_turn(), timeout)
        except Exception:  # timeout, no claude CLI, a crash: all mean "not usable yet"
            log.debug("login check failed", exc_info=True)
            return "login"
        finally:
            try:
                await asyncio.wait_for(brain.close(), 15)
            except Exception:
                log.debug("closing the login-check session failed", exc_info=True)


# ---- 2. microphone ----

async def measure_mic(device: str | int | None, seconds: float = MIC_SECONDS,
                   audio_factory: Callable[..., Any] | None = None) -> float | None:
    """Peak level over ``seconds`` of input; None if the mic could not be opened at all."""
    if audio_factory is None:
        from zundamon_jarvis.audio_in import AudioIn as audio_factory
    import numpy as np

    audio = audio_factory(device=device)
    try:
        audio.open()
    except Exception:
        log.debug("mic open failed", exc_info=True)
        return None
    peak = 0.0

    async def collect() -> None:
        nonlocal peak
        async for frame in audio.frames():
            peak = max(peak, float(np.max(np.abs(frame))))

    try:
        await asyncio.wait_for(collect(), seconds)
    except TimeoutError:
        pass
    finally:
        audio.close()
    return peak


# ---- 4. voice ----

def engine_command(run: Path, sys_: str) -> list[str]:
    """The engine started just for the voice samples. Windows' DirectML build runs on the CPU
    unless asked for the GPU."""
    cmd = [str(run), "--host", "127.0.0.1", "--port", "50021"]
    return cmd + ["--use_gpu"] if sys_ == "windows" else cmd


def _detached(sys_: str) -> dict:
    """Popen options: no console window on Windows; its own session elsewhere (POSIX only), so
    Ctrl-C in the terminal reaches setup, which then stops the engine itself."""
    if sys_ == "windows":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


@contextlib.asynccontextmanager
async def open_voice_engine(say: Callable[..., None], system: str | None = None,
                       url: str = DEFAULT_URL, wait_s: float = ENGINE_WAIT_S) -> AsyncIterator[Any]:
    """A VOICEVOX client, or None if no engine answers. When none is running yet (the first
    setup: the services start at the end) the installed engine is started just for this."""
    from zundamon_jarvis import engine
    from zundamon_jarvis.tts import Voicevox

    vv = Voicevox(url)
    proc: subprocess.Popen | None = None

    async def up() -> bool:
        try:
            await vv.version()
            return True
        except Exception:
            return False

    try:
        ok = await up()
        sys_ = system or platform.system()
        run = engine.run_path(sys_)
        if not ok and run.is_file():
            say("VOICEVOX を起動しています（少しかかります）…")
            proc = subprocess.Popen(engine_command(run, sys_), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, **_detached(sys_))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_s
            while not (ok := await up()) and proc.poll() is None and loop.time() < deadline:
                await asyncio.sleep(1)
        yield vv if ok else None
    finally:
        await vv.aclose()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


async def play_pcm(pcm: Any) -> None:
    from zundamon_jarvis.player import Player

    await asyncio.wait_for(Player().play(pcm, RATE), 15)


# ---- 7. services ----

def install_and_start(system: str | None = None) -> None:
    from zundamon_jarvis import service

    sys_ = system or platform.system()
    # macOS: bootout + bootstrap, so a re-run restarts with the new config; Windows: the
    # service module restarts the supervisor itself.
    service.install_services(sys_)
    if sys_ == "linux":  # already-installed units are left as they are: restart to load the new config
        subprocess.run(["systemctl", "--user", "restart", "zundamon-jarvis.service"], check=True)


def _request(cmd: str, timeout: float = 3.0) -> dict:
    from zundamon_jarvis.control import request

    return request(cmd, timeout=timeout)


# ---- step helpers shared with the web setup (zundamon_jarvis.setup_gui) ----

def _tilde(path: Path) -> str:
    home = Path.home()
    if path == home:
        return "~"
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def headphones_note(sys_: str) -> str:
    return {"mac": HEADPHONES_NOTE, "windows": WINDOWS_HEADPHONES_NOTE}.get(sys_, LINUX_HEADPHONES_NOTE)


def mic_problem_note(sys_: str, problem: str) -> str:
    """What to do when the mic could not be opened (``problem`` "open") or read only silence
    ("silent"). macOS: the grant belongs to the app setup runs under (the terminal)."""
    if sys_ == "mac":
        return TERMINAL_MIC_NOTE
    if sys_ == "windows":
        return WINDOWS_MIC_NOTE
    return ("マイクがつながっているか確かめてください。" if problem == "open"
            else "マイクがミュートになっていないか確かめてください。")


def read_config(path: Path) -> tuple[Config | None, str | None]:
    """The config, or (None, what to tell the user) when ``config.toml`` cannot be parsed."""
    try:
        return load_config(path), None
    except ValueError as e:  # a broken config.toml (TOMLDecodeError is a ValueError)
        return None, (f"設定ファイル {_tilde(path)} が読めません（{e}）。"
                      "直すか、消してから、もう一度 `zundamon-jarvis setup` を実行してください。")


def voice_choices(styles: list[tuple[str, str, int]]) -> list[tuple[str, int]]:
    """(label, speaker_id) of the ``VOICES`` the engine has, in ``VOICES`` order."""
    return [(f"{n} {s}", sid) for name, style in VOICES
            for n, s, sid in styles if (n, s) == (name, style)]


def save_voice(speaker_id: int, cfg: Config, path: Path) -> None:
    update_config({"speaker_id": speaker_id}, path)
    cfg.speaker_id = speaker_id


def resolve_folder(raw: str, cwd: Path | None = None) -> Path | None:
    """The existing directory a typed (or dragged) path names, else None. ``~`` is expanded and a
    relative path is taken from ``cwd`` (default: the current directory)."""
    s = clean_path_input(raw)
    if not s:
        return None
    p = Path(s).expanduser()
    p = p if p.is_absolute() else (cwd or Path.cwd()) / p
    return p if p.is_dir() else None


def save_knowledge_dir(folder: Path | None, cfg: Config, path: Path) -> None:
    """Set (stored with ``~`` so the file stays portable) or, with None, remove the folder."""
    update_config({"knowledge_dir": None if folder is None else _tilde(folder)}, path)
    cfg.knowledge_dir = folder


def save_model(model: str, cfg: Config, path: Path) -> None:
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}: {model!r}")
    update_config({"model": model}, path)
    cfg.model = model


def pause_daemon(request: Callable[..., dict]) -> bool:
    """A re-run: keep the running daemon from answering the voice samples. True if this paused it
    (one the user left paused, or none running, is left alone)."""
    try:
        if request("status", timeout=2).get("state") != "paused":
            request("pause", timeout=2)
            return True
    except (OSError, ValueError):
        pass
    return False


def resume_daemon(request: Callable[..., dict]) -> None:
    with contextlib.suppress(OSError, ValueError):
        request("resume", timeout=2)


def start_failed_message(e: BaseException) -> str:
    return (f"常駐の設定ができませんでした（{e}）。"
            "インストールを最後までやり直してから、もう一度 `zundamon-jarvis setup` を実行してください。")


START_ERRORS = (RuntimeError, OSError, subprocess.CalledProcessError)


async def wait_for_status(request: Callable[..., dict], sleep: Callable[[float], Awaitable[None]],
                          tries: int = STATUS_WAIT_S) -> dict | None:
    """The freshly started daemon's status once its control socket answers (None: not in time)."""
    for _ in range(tries):
        try:
            return request("status", timeout=1)
        except (OSError, ValueError):
            await sleep(1)
    return None


DaemonMic = str  # "ok" (sound arrives) | "silent" (frames, all zeros: no grant) | "unknown"


async def probe_daemon_mic(request: Callable[..., dict], sleep: Callable[[float], Awaitable[None]],
                           system: str = "mac") -> DaemonMic:
    """Listen to the daemon's own mic for ``DAEMON_MIC_CHECK_S`` seconds. On macOS the daemon is
    a different program (uv's python3.12) with its own mic grant, so the terminal's says nothing;
    Windows can refuse desktop apps the mic as a whole."""
    from zundamon_jarvis.doctor import mic_verdict

    info = None
    for _ in range(DAEMON_MIC_CHECK_S):
        await sleep(1)
        try:
            info = request("debug", timeout=2).get("debug")
        except (OSError, ValueError, AttributeError):
            info = None
        if isinstance(info, dict) and (info.get("max_abs_last_5s") or 0) > 0:
            return "ok"
    silent = mic_verdict(info if isinstance(info, dict) else None, system=system)
    return "silent" if silent else "unknown"


DAEMON_MIC_ASK = ("常駐しているほうのマイクも確かめます。"
                  "「\"python3.12\" がマイクへのアクセスを求めています」と出たら「許可」を押して、"
                  f"何か話しかけてください（{DAEMON_MIC_CHECK_S} 秒ほど聞きます）…")
DAEMON_MIC_OK = "常駐しているほうにも音が届いています。"
DAEMON_MIC_UNKNOWN = ("常駐しているほうのマイクはまだ確かめられませんでした（起動の途中かもしれません）。"
                      "しばらくしてもメニューバーが 📴 のままなら `zundamon-jarvis doctor | pbcopy` の結果を送ってください。")


def daemon_mic_text(sys_: str, what: str) -> str:
    """What to say for ``what``: "ask" (before listening) or a ``probe_daemon_mic`` verdict."""
    if sys_ != "windows":
        return {"ask": DAEMON_MIC_ASK, "ok": DAEMON_MIC_OK, "unknown": DAEMON_MIC_UNKNOWN,
                "silent": DAEMON_MIC_NOTE}[what]
    return {
        "ask": f"常駐しているほうのマイクも確かめます。何か話しかけてください（{DAEMON_MIC_CHECK_S} 秒ほど聞きます）…",
        "ok": DAEMON_MIC_OK,
        "unknown": ("常駐しているほうのマイクはまだ確かめられませんでした（起動の途中かもしれません）。"
                    "しばらくしても通知領域のアイコンが使えないままなら `zundamon-jarvis doctor | clip` の結果を送ってください。"),
        "silent": WINDOWS_DAEMON_MIC_NOTE,
    }[what]


# ---- the wizard ----

async def _ask(con: Console, prompt: str) -> str:
    # Called on the loop thread on purpose: Ctrl-C then interrupts input() at once (a worker
    # thread would keep asyncio.run waiting for Enter). Nothing else runs while asking.
    return con.ask(prompt).strip()


def _voice_label(styles: list[tuple[str, str, int]], speaker_id: int | None) -> str:
    if speaker_id is None:
        return f"{DEFAULT_SPEAKER} ノーマル（既定）"
    return next((f"{n} {s}" for n, s, sid in styles if sid == speaker_id), f"番号 {speaker_id} の声")


async def _voice_step(con: Console, cfg: Config, path: Path, engine_cm, play) -> None:
    async with engine_cm(con.say) as vv:
        speakers = None
        if vv is not None:
            try:
                speakers = await vv.speakers()
            except Exception:
                log.debug("VOICEVOX /speakers failed", exc_info=True)
        if speakers is None:
            con.say("VOICEVOX につながらないので、声選びは飛ばします。"
                    "あとでメニューバーの Settings… から選べます。")
            return
        styles = talk_styles(speakers)
        choices = voice_choices(styles)
        current = _voice_label(styles, cfg.speaker_id)
        while True:
            con.say(f"今の声: {current}")
            for i, (label, _) in enumerate(choices, 1):
                con.say(f"  {i}) {label}")
            con.say("  0) 今のまま")
            ans = await _ask(con, "番号を入れると試しに読みます（Enter で今のまま）: ")
            if ans in ("", "0"):
                return
            if not (ans.isdigit() and 1 <= int(ans) <= len(choices)):
                con.say(f"0〜{len(choices)} の番号を入れてください。")
                continue
            label, sid = choices[int(ans) - 1]
            try:
                await play(await vv.synth(SAMPLE, sid, speed=cfg.speed))
            except Exception as e:
                con.say(f"試し読みできませんでした（{e}）")
            if (await _ask(con, f"{label} にしますか？ [Y/n]: ")).lower() in YES:
                save_voice(sid, cfg, path)
                con.say(f"声を {label} にしました。")
                return


def clean_path_input(raw: str, system: str | None = None) -> str:
    """A path as typed or dragged into a terminal: surrounding quotes removed, or (unquoted, on
    macOS and Linux) backslash escapes such as ``\\ `` undone. On Windows a backslash is the
    path separator (``C:\\Users\\...``), so it is left alone there."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    if (system or platform.system()) == "windows":
        return s
    return re.sub(r"\\(.)", r"\1", s)


async def _knowledge_step(con: Console, cfg: Config, path: Path) -> None:
    con.say("メモやノートのフォルダがあれば、その中身も見ながら答えます（なくても使えます）。")
    cur = cfg.knowledge_dir
    prompt = (f"フォルダの場所（今: {_tilde(cur)}。Enter でそのまま、- で外す）: " if cur
              else "フォルダの場所（Enter で飛ばす）: ")
    while True:
        ans = await _ask(con, prompt)
        if not ans:
            return
        if ans == "-" and cur is not None:
            save_knowledge_dir(None, cfg, path)
            con.say("知識フォルダを外しました。")
            return
        p = resolve_folder(ans)
        if p is None:
            con.say(f"「{ans}」というフォルダが見つかりません。もう一度入れてください。")
            continue
        save_knowledge_dir(p, cfg, path)
        con.say(f"知識フォルダ: {_tilde(p)}")
        return


async def _model_step(con: Console, cfg: Config, path: Path) -> None:
    cur = cfg.model or "Claude の既定"
    while True:
        ans = (await _ask(con, f"Claude のモデル（{' / '.join(MODELS)}。今: {cur}。Enter でそのまま）: ")).lower()
        model = ans or cfg.model
        if model is None:
            return  # an explicit "use the CLI default" stays as it is
        if model in MODELS:
            save_model(model, cfg, path)
            con.say(f"モデル: {model}")
            return
        con.say(f"{' / '.join(MODELS)} のどれかを入れてください。")


async def _mic_step(con: Console, cfg: Config, sys_: str, mic_peak) -> None:
    peak = await mic_peak(cfg.mic_device)
    if (peak is None or peak <= 0.0) and sys_ == "mac":
        # The first open brings up the permission dialog; until "許可" is pressed it reads zeros.
        con.say("まだ音が取れていません。マイクの許可を求める画面が出ていたら「許可」を押してください。")
        if (await _ask(con, "許可を押したら Enter（s で飛ばす）: ")).lower() != "s":
            peak = await mic_peak(cfg.mic_device)
    if peak is None:
        con.say("マイクを開けませんでした。")
        con.say(mic_problem_note(sys_, "open"))
    elif peak <= 0.0:
        con.say("音が取れませんでした（ずっと無音）。")
        con.say(mic_problem_note(sys_, "silent"))
    else:
        con.say(f"音が取れました（いちばん大きい音: {peak:.3f}）")


async def _daemon_mic_check(con: Console, request, sleep, sys_: str = "mac") -> None:
    """macOS / Windows: the daemon is a different program with its own mic grant."""
    con.say(daemon_mic_text(sys_, "ask"))
    for attempt in range(2):
        verdict = await probe_daemon_mic(request, sleep, sys_)
        con.say(daemon_mic_text(sys_, verdict))
        if verdict != "silent":
            return
        if attempt or (await _ask(con, "直したら Enter でもう一度確かめます（s で飛ばす）: ")).lower() == "s":
            return


async def run_setup(
    con: Console,
    *,
    config_path: Path | None = None,
    system: str | None = None,
    login_check: Callable[[str | None], Awaitable[LoginResult]] = claude_login_check,
    mic_peak: Callable[[str | int | None], Awaitable[float | None]] = measure_mic,
    voice_engine: Callable[..., Any] | None = None,
    play: Callable[[Any], Awaitable[None]] = play_pcm,
    start_services: Callable[[], None] | None = None,
    request: Callable[..., dict] = _request,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    path = config_path or config_mod.CONFIG_PATH
    sys_ = system or platform.system()
    cfg, problem = read_config(path)
    if cfg is None:
        con.say(problem)
        return 1
    engine_cm = voice_engine or (lambda say: open_voice_engine(say, sys_))
    start = start_services or (lambda: install_and_start(sys_))

    con.say("[1/7] Claude にログインできているか確かめます…")
    login = await login_check(cfg.model)
    if login == "login":
        con.say(LOGIN_HELP)
        return EXIT_LOGIN
    con.say(LIMIT_NOTE if login == "limit" else "Claude: OK")

    paused = pause_daemon(request)
    started = False
    try:
        con.say("")
        con.say("[2/7] マイクを確かめます（Mac では許可を求める画面が出たら「許可」を押してください）…")
        await _mic_step(con, cfg, sys_, mic_peak)

        con.say("")
        con.say("[3/7] " + headphones_note(sys_))

        con.say("")
        con.say("[4/7] 声を選びます")
        await _voice_step(con, cfg, path, engine_cm, play)

        con.say("")
        con.say("[5/7] 知識フォルダ（任意）")
        await _knowledge_step(con, cfg, path)

        con.say("")
        con.say("[6/7] モデル")
        await _model_step(con, cfg, path)

        con.say("")
        con.say("[7/7] 常駐の設定をして起動します…")
        try:
            start()
        except START_ERRORS as e:
            con.say(start_failed_message(e))
            return 1
        started = True
        status = await wait_for_status(request, sleep)
        if status and "text" in status:
            con.say(f"起動しました（今: {status['text']}）。")
            if sys_ in DAEMON_MIC_SYSTEMS:
                await _daemon_mic_check(con, request, sleep, sys_)
            con.say("話しかけてみてください。")
        else:
            con.say("起動中です。少し待ってからメニューバーのアイコンを見てください。")
        con.say(f"設定ファイル: {_tilde(path)}")
        return 0
    finally:
        if paused and not started:
            resume_daemon(request)


def main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis setup --cli` (dispatched by zundamon_jarvis.cli)."""
    if argv:
        print("usage: zundamon-jarvis setup --cli", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_setup(Console()))
    except (KeyboardInterrupt, EOFError):
        print("\n中断しました。もう一度 `zundamon-jarvis setup` を実行すると、続きから設定できます。")
        return 130


# ---- `zundamon-jarvis uninstall` ----

def run_uninstall(
    con: Console,
    *,
    system: str | None = None,
    uninstall: Callable[[str], None] | None = None,
    engine_dir: Path | None = None,
    config_path: Path | None = None,
) -> int:
    from zundamon_jarvis import engine, service

    sys_ = system or platform.system()
    con.say("常駐を止めて、自動起動の設定を外します…")
    (uninstall or service.uninstall_services)(sys_)
    con.say("外しました。")
    engine_dir = engine_dir or engine.default_dest(sys_)
    if engine_dir.is_symlink() or engine_dir.exists():
        if con.ask(f"VOICEVOX エンジン（{_tilde(engine_dir)}）も消しますか？ [y/N]: ").strip().lower() in YES[1:]:
            if engine_dir.is_symlink():
                engine_dir.unlink()
            else:
                shutil.rmtree(engine_dir)
            con.say("エンジンを消しました。")
    config_path = config_path or platform.config_dir(sys_) / "config.toml"
    if config_path.exists():
        if con.ask(f"設定（{_tilde(config_path)}）も消しますか？ [y/N]: ").strip().lower() in YES[1:]:
            config_path.unlink()
            con.say("設定を消しました。")
    from zundamon_jarvis.stt import model_cache_dir

    con.say("本体は `uv tool uninstall zundamon-jarvis` で外せます。"
            f"会話のログは {_tilde(platform.log_dir(sys_))} に残っています（14 日で消えます）。")
    con.say(f"文字起こしのモデル（数 GB）は {_tilde(model_cache_dir(sys_))} にあります。"
            "いらなければ、そのフォルダを消してください。")
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis uninstall` (dispatched by zundamon_jarvis.cli)."""
    if argv:
        print("usage: zundamon-jarvis uninstall", file=sys.stderr)
        return 2
    try:
        return run_uninstall(Console())
    except (KeyboardInterrupt, EOFError):
        print("\n中断しました。")
        return 130
