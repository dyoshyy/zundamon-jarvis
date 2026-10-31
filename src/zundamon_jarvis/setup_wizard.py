"""`zundamon-jarvis setup`: the first-run questions, in order.

1. Claude login (one short turn through ``ClaudeBrain``; no reply in 60 s -> exit 2)
2. Microphone (open it for 1 s: on a Mac this brings up the permission dialog)
3. Headphones advice
4. Voice (a few candidates, previewed by number)
5. ``knowledge_dir`` (optional)
6. Claude model
7. Install the services and start them

Every answer is written to ``config.toml`` as soon as it is given; a re-run offers the current
values (Enter keeps them). Everything outside (input/output, Claude, the mic, VOICEVOX,
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
DAEMON_MIC_CHECK_S = 5
HEADPHONES_NOTE = (
    "ヘッドホン（イヤホン）での利用をおすすめします。Mac のスピーカーから声を出すと、"
    "マイクがその声を拾ってしまうので、話している間は聞き取りを止めます（途中で割り込めません）。"
    "ヘッドホンなら、しゃべっている途中でも話しかけて止められます。")
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
        run = engine.run_path(system or platform.system())
        if not ok and run.is_file():
            say("VOICEVOX を起動しています（少しかかります）…")
            proc = subprocess.Popen([str(run), "--host", "127.0.0.1", "--port", "50021"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
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
    service.install_services(sys_)  # macOS: bootout + bootstrap, so a re-run restarts with the new config
    if sys_ == "linux":  # already-installed units are left as they are: restart to load the new config
        subprocess.run(["systemctl", "--user", "restart", "zundamon-jarvis.service"], check=True)


def _request(cmd: str, timeout: float = 3.0) -> dict:
    from zundamon_jarvis.control import request

    return request(cmd, timeout=timeout)


# ---- the wizard ----

def _tilde(path: Path) -> str:
    home = Path.home()
    if path == home:
        return "~"
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


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
        choices = [(f"{n} {s}", sid) for name, style in VOICES
                   for n, s, sid in styles if (n, s) == (name, style)]
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
                update_config({"speaker_id": sid}, path)
                cfg.speaker_id = sid
                con.say(f"声を {label} にしました。")
                return


def clean_path_input(raw: str) -> str:
    """A path as typed or dragged into Terminal: surrounding quotes removed, or (unquoted)
    backslash escapes such as ``\\ `` undone."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
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
            update_config({"knowledge_dir": None}, path)
            cfg.knowledge_dir = None
            con.say("知識フォルダを外しました。")
            return
        p = Path(clean_path_input(ans)).expanduser()
        p = p if p.is_absolute() else Path.cwd() / p
        if not p.is_dir():
            con.say(f"「{ans}」というフォルダが見つかりません。もう一度入れてください。")
            continue
        update_config({"knowledge_dir": _tilde(p)}, path)
        cfg.knowledge_dir = p
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
            update_config({"model": model}, path)
            cfg.model = model
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
        con.say(TERMINAL_MIC_NOTE if sys_ == "mac" else "マイクがつながっているか確かめてください。")
    elif peak <= 0.0:
        con.say("音が取れませんでした（ずっと無音）。")
        con.say(TERMINAL_MIC_NOTE if sys_ == "mac" else "マイクがミュートになっていないか確かめてください。")
    else:
        con.say(f"音が取れました（いちばん大きい音: {peak:.3f}）")


async def _daemon_mic_check(con: Console, request, sleep) -> None:
    """macOS: the daemon is a different program (uv's python3.12) with its own mic grant."""
    from zundamon_jarvis.doctor import mic_verdict

    con.say("常駐しているほうのマイクも確かめます。"
            "「\"python3.12\" がマイクへのアクセスを求めています」と出たら「許可」を押して、"
            f"何か話しかけてください（{DAEMON_MIC_CHECK_S} 秒ほど聞きます）…")
    for attempt in range(2):
        info = None
        for _ in range(DAEMON_MIC_CHECK_S):
            await sleep(1)
            try:
                info = request("debug", timeout=2).get("debug")
            except (OSError, ValueError, AttributeError):
                info = None
            if isinstance(info, dict) and (info.get("max_abs_last_5s") or 0) > 0:
                con.say("常駐しているほうにも音が届いています。")
                return
        if not mic_verdict(info if isinstance(info, dict) else None):
            con.say("常駐しているほうのマイクはまだ確かめられませんでした（起動の途中かもしれません）。"
                    "しばらくしてもメニューバーが 📴 のままなら `zundamon-jarvis doctor | pbcopy` の結果を送ってください。")
            return
        con.say(DAEMON_MIC_NOTE)
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
    try:
        cfg = load_config(path)
    except ValueError as e:  # a broken config.toml (TOMLDecodeError is a ValueError)
        con.say(f"設定ファイル {_tilde(path)} が読めません（{e}）。"
                "直すか、消してから、もう一度 `zundamon-jarvis setup` を実行してください。")
        return 1
    engine_cm = voice_engine or (lambda say: open_voice_engine(say, sys_))
    start = start_services or (lambda: install_and_start(sys_))

    con.say("[1/7] Claude にログインできているか確かめます…")
    login = await login_check(cfg.model)
    if login == "login":
        con.say(LOGIN_HELP)
        return EXIT_LOGIN
    con.say(LIMIT_NOTE if login == "limit" else "Claude: OK")

    paused = False
    try:  # a re-run: keep the running daemon from answering the voice samples
        if request("status", timeout=2).get("state") != "paused":  # left paused by the user: leave it
            request("pause", timeout=2)
            paused = True
    except (OSError, ValueError):
        pass
    started = False
    try:
        con.say("")
        con.say("[2/7] マイクを確かめます（Mac では許可を求める画面が出たら「許可」を押してください）…")
        await _mic_step(con, cfg, sys_, mic_peak)

        con.say("")
        con.say("[3/7] " + HEADPHONES_NOTE)

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
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            con.say(f"常駐の設定ができませんでした（{e}）。"
                    "インストールを最後までやり直してから、もう一度 `zundamon-jarvis setup` を実行してください。")
            return 1
        started = True
        status = None
        for _ in range(STATUS_WAIT_S):
            try:
                status = request("status", timeout=1)
                break
            except (OSError, ValueError):
                await sleep(1)
        if status and "text" in status:
            con.say(f"起動しました（今: {status['text']}）。")
            if sys_ == "mac":
                await _daemon_mic_check(con, request, sleep)
            con.say("話しかけてみてください。")
        else:
            con.say("起動中です。少し待ってからメニューバーのアイコンを見てください。")
        con.say(f"設定ファイル: {_tilde(path)}")
        return 0
    finally:
        if paused and not started:
            with contextlib.suppress(OSError, ValueError):
                request("resume", timeout=2)


def main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis setup` (dispatched by zundamon_jarvis.cli)."""
    if argv:
        print("usage: zundamon-jarvis setup", file=sys.stderr)
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
