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
from zundamon_jarvis import i18n, platform
from zundamon_jarvis.brain import AuthProblem, PermissionRequest, TurnEnd, UsageLimit
from zundamon_jarvis.config import MODELS, Config, load_config, update_config
from zundamon_jarvis.control import status_label
from zundamon_jarvis.tts import DEFAULT_SPEAKER, DEFAULT_URL, RATE, talk_styles

log = logging.getLogger(__name__)

EXIT_LOGIN = 2
LOGIN_PROMPT = "OKとだけ返して"
LOGIN_TIMEOUT_S = 60.0
MIC_SECONDS = 1.0
DAEMON_MIC_CHECK_S = 5
DAEMON_MIC_SYSTEMS = ("mac", "windows")  # where an app can be refused the mic: frames of zeros

# What setup shows (the terminal wizard and, through the step helpers, the web page), in both
# display languages (zundamon_jarvis.i18n). The Japanese is the original wording.
TEXT = {
    "login_help": {
        "ja": "Claude にログインできていません。"
              "ターミナルで `claude` を実行してログインしてから、もう一度 `zundamon-jarvis setup` を実行してください",
        "en": "You're not signed in to Claude. Run `claude` in a terminal to sign in, "
              "then run `zundamon-jarvis setup` again",
    },
    "limit_note": {
        "ja": "Claude にはログインできていますが、いまは利用上限に達しています。"
              "設定はこのまま続けられます。上限が戻れば（時間をおけば）答えるようになります。",
        "en": "You're signed in to Claude, but you've hit the usage limit for now. "
              "You can finish setting up; it answers again once the limit resets.",
    },
    # The probe runs as a child of the terminal app, so this grant is the terminal's.
    "terminal_mic_note": {
        "ja": "システム設定 > プライバシーとセキュリティ > マイク で、"
              "使っているターミナル（「ターミナル」など）をオンにしてください。",
        "en": "In System Settings > Privacy & Security > Microphone, "
              "turn on the terminal app you're using (Terminal, for example).",
    },
    # The LaunchAgent daemon runs uv's own python3.12, which macOS asks about (and lists) separately.
    "daemon_mic_note": {
        "ja": """常駐しているほう（バックグラウンドの zundamon-jarvis）にマイクの音が届いていません。
システム設定 > プライバシーとセキュリティ > マイク で「python3.12」を探して、オンにしてください。
一覧にないときは、ターミナルで次の 2 行を実行してから、もう一度話しかけてください
（1 行目はほかのアプリのマイクの許可も取り消すので、次に使うときにまた聞かれます）:
  tccutil reset Microphone
  launchctl kickstart -k gui/$UID/dev.zundamon-jarvis.daemon
それでも音が届かないときは `zundamon-jarvis doctor | pbcopy` の結果を送ってください。""",
        "en": """The background zundamon-jarvis isn't getting any sound from the microphone.
In System Settings > Privacy & Security > Microphone, find “python3.12” and turn it on.
If it isn't listed, run these two lines in a terminal, then talk to it again
(the first line also resets other apps' microphone permissions; they'll ask again next time):
  tccutil reset Microphone
  launchctl kickstart -k gui/$UID/dev.zundamon-jarvis.daemon
Still no sound? Send the output of `zundamon-jarvis doctor | pbcopy`.""",
    },
    "windows_daemon_mic_note": {
        "ja": """常駐しているほう（バックグラウンドの zundamon-jarvis）にマイクの音が届いていません。
設定 > プライバシーとセキュリティ > マイク で「マイクへのアクセス」と
「デスクトップ アプリにマイクへのアクセスを許可する」をオンにしてから、もう一度話しかけてください。
それでも音が届かないときは `zundamon-jarvis doctor | clip` の結果を送ってください。""",
        "en": """The background zundamon-jarvis isn't getting any sound from the microphone.
In Settings > Privacy & security > Microphone, turn on “Microphone access” and
“Let desktop apps access your microphone”, then talk to it again.
Still no sound? Send the output of `zundamon-jarvis doctor | clip`.""",
    },
    "windows_mic_note": {
        "ja": "設定 > プライバシーとセキュリティ > マイク で「マイクへのアクセス」と"
              "「デスクトップ アプリにマイクへのアクセスを許可する」をオンにしてください。",
        "en": "In Settings > Privacy & security > Microphone, turn on “Microphone access” and "
              "“Let desktop apps access your microphone”.",
    },
    "mic_check_connected": {"ja": "マイクがつながっているか確かめてください。",
                            "en": "Check that the microphone is connected."},
    "mic_check_muted": {"ja": "マイクがミュートになっていないか確かめてください。",
                        "en": "Check that the microphone isn't muted."},
    "headphones_mac": {
        "ja": "ヘッドホン（イヤホン）での利用をおすすめします。Mac のスピーカーから声を出すと、"
              "マイクがその声を拾ってしまうので、話している間は聞き取りを止めます（途中で割り込めません）。"
              "ヘッドホンなら、しゃべっている途中でも話しかけて止められます。",
        "en": "Headphones (or earbuds) are recommended. When the voice comes out of the Mac's speakers, "
              "the mic picks it up, so it stops listening while it talks (you can't cut in). "
              "With headphones you can talk over it to stop it.",
    },
    # Windows has no echo cancellation either; the daemon cannot tell its speakers apart from headphones.
    "headphones_windows": {
        "ja": "ヘッドホン（イヤホン）での利用をおすすめします。Windows にはエコーキャンセルがないので、"
              "PC のスピーカーから声を出すと、マイクがずんだもんの声を拾ってしまいます。"
              "ヘッドホンなら、しゃべっている途中でも話しかけて止められます。",
        "en": "Headphones (or earbuds) are recommended. Windows has no echo cancellation, so through "
              "the PC's speakers the mic picks up Zundamon's own voice. "
              "With headphones you can talk over it to stop it.",
    },
    "headphones_linux": {
        "ja": "ヘッドホン（イヤホン）での利用をおすすめします。スピーカーで使うときは、PipeWire のエコーキャンセル"
              "（リポジトリの deploy/install-audio.sh）を入れて、マイクに「zundamon_jarvis_aec_source」を選んでください。"
              "どちらでも、しゃべっている途中に話しかけて止められます。",
        "en": "Headphones (or earbuds) are recommended. For speakers, set up PipeWire echo cancellation "
              "(deploy/install-audio.sh in the repository) and choose “zundamon_jarvis_aec_source” as the "
              "microphone. Either way, you can talk over it to stop it.",
    },
    "config_unreadable": {
        "ja": "設定ファイル {path} が読めません（{error}）。"
              "直すか、消してから、もう一度 `zundamon-jarvis setup` を実行してください。",
        "en": "Can't read the settings file {path} ({error}). "
              "Fix or delete it, then run `zundamon-jarvis setup` again.",
    },
    "start_failed": {
        "ja": "常駐の設定ができませんでした（{error}）。"
              "インストールを最後までやり直してから、もう一度 `zundamon-jarvis setup` を実行してください。",
        "en": "Couldn't set up the background service ({error}). "
              "Run the installer again all the way through, then run `zundamon-jarvis setup` again.",
    },
    "daemon_mic_ask_mac": {
        "ja": "常駐しているほうのマイクも確かめます。"
              "「\"python3.12\" がマイクへのアクセスを求めています」と出たら「許可」を押して、"
              "何か話しかけてください（{seconds} 秒ほど聞きます）…",
        "en": "Now checking the background app's microphone. "
              "If you see “\"python3.12\" would like to access the microphone”, click Allow, "
              "then say something (it listens for about {seconds} seconds)…",
    },
    "daemon_mic_ask_windows": {
        "ja": "常駐しているほうのマイクも確かめます。何か話しかけてください（{seconds} 秒ほど聞きます）…",
        "en": "Now checking the background app's microphone. "
              "Say something (it listens for about {seconds} seconds)…",
    },
    "daemon_mic_ok": {"ja": "常駐しているほうにも音が届いています。",
                      "en": "The background app hears you too."},
    "daemon_mic_unknown_mac": {
        "ja": "常駐しているほうのマイクはまだ確かめられませんでした（起動の途中かもしれません）。"
              "しばらくしてもメニューバーが 📴 のままなら `zundamon-jarvis doctor | pbcopy` の結果を送ってください。",
        "en": "Couldn't check the background app's microphone yet (it may still be starting). "
              "If the menu bar still shows 📴 after a while, send the output of `zundamon-jarvis doctor | pbcopy`.",
    },
    "daemon_mic_unknown_windows": {
        "ja": "常駐しているほうのマイクはまだ確かめられませんでした（起動の途中かもしれません）。"
              "しばらくしても通知領域のアイコンが使えないままなら `zundamon-jarvis doctor | clip` の結果を送ってください。",
        "en": "Couldn't check the background app's microphone yet (it may still be starting). "
              "If the notification area icon still doesn't work after a while, "
              "send the output of `zundamon-jarvis doctor | clip`.",
    },
    "engine_starting": {"ja": "VOICEVOX を起動しています（少しかかります）…",
                        "en": "Starting VOICEVOX (this takes a moment)…"},
    "voice_default": {"ja": "{speaker} ノーマル（既定）", "en": "{speaker} ノーマル (default)"},
    "voice_number": {"ja": "番号 {id} の声", "en": "Voice #{id}"},
    "preview_failed": {"ja": "試し読みできませんでした（{error}）", "en": "Couldn't play the sample ({error})"},
    # the terminal wizard
    "voice_skip": {"ja": "VOICEVOX につながらないので、声選びは飛ばします。"
                         "あとでメニューバーの「設定…」から選べます。",
                   "en": "Can't reach VOICEVOX, so skipping the voice for now. "
                         "You can choose one later in Settings… in the menu bar."},
    "voice_current": {"ja": "今の声: {voice}", "en": "Current voice: {voice}"},
    "voice_keep": {"ja": "  0) 今のまま", "en": "  0) Keep it"},
    "voice_ask": {"ja": "番号を入れると試しに読みます（Enter で今のまま）: ",
                  "en": "Type a number to hear it (Enter keeps the current one): "},
    "voice_bad": {"ja": "0〜{n} の番号を入れてください。", "en": "Type a number from 0 to {n}."},
    "voice_confirm": {"ja": "{voice} にしますか？ [Y/n]: ", "en": "Use {voice}? [Y/n]: "},
    "voice_saved": {"ja": "声を {voice} にしました。", "en": "Voice set to {voice}."},
    "knowledge_about": {"ja": "メモやノートのフォルダがあれば、その中身も見ながら答えます（なくても使えます）。",
                        "en": "If you keep notes in a folder, it can look through them when it answers (optional)."},
    "knowledge_ask_current": {"ja": "フォルダの場所（今: {path}。Enter でそのまま、- で外す）: ",
                              "en": "Folder (now: {path}; Enter keeps it, - removes it): "},
    "knowledge_ask": {"ja": "フォルダの場所（Enter で飛ばす）: ", "en": "Folder (Enter to skip): "},
    "knowledge_removed": {"ja": "知識フォルダを外しました。", "en": "Knowledge folder removed."},
    "folder_not_found_retry": {"ja": "「{path}」というフォルダが見つかりません。もう一度入れてください。",
                               "en": "There's no folder “{path}”. Try again."},
    "knowledge_set": {"ja": "知識フォルダ: {path}", "en": "Knowledge folder: {path}"},
    "model_default": {"ja": "Claude の既定", "en": "Claude's default"},
    "model_ask": {"ja": "Claude のモデル（{models}。今: {current}。Enter でそのまま）: ",
                  "en": "Claude model ({models}; now: {current}; Enter keeps it): "},
    "model_set": {"ja": "モデル: {model}", "en": "Model: {model}"},
    "model_bad": {"ja": "{models} のどれかを入れてください。", "en": "Type one of {models}."},
    "mic_allow_hint": {"ja": "まだ音が取れていません。マイクの許可を求める画面が出ていたら「許可」を押してください。",
                       "en": "No sound yet. If a dialog asks for microphone access, click Allow."},
    "mic_allow_ask": {"ja": "許可を押したら Enter（s で飛ばす）: ",
                      "en": "Press Enter once you've allowed it (s to skip): "},
    "mic_open_failed": {"ja": "マイクを開けませんでした。", "en": "Couldn't open the microphone."},
    "mic_silent": {"ja": "音が取れませんでした（ずっと無音）。", "en": "No sound came in (silence the whole time)."},
    "mic_ok": {"ja": "音が取れました（いちばん大きい音: {peak}）", "en": "Sound is coming in (loudest: {peak})"},
    "daemon_mic_retry": {"ja": "直したら Enter でもう一度確かめます（s で飛ばす）: ",
                         "en": "Once it's fixed, press Enter to check again (s to skip): "},
    "step_login": {"ja": "[1/7] Claude にログインできているか確かめます…", "en": "[1/7] Checking your Claude login…"},
    "claude_ok": {"ja": "Claude: OK", "en": "Claude: OK"},
    "step_mic": {"ja": "[2/7] マイクを確かめます（Mac では許可を求める画面が出たら「許可」を押してください）…",
                 "en": "[2/7] Checking the microphone (on a Mac, click Allow if it asks)…"},
    "step_headphones": {"ja": "[3/7] {note}", "en": "[3/7] {note}"},
    "step_voice": {"ja": "[4/7] 声を選びます", "en": "[4/7] Choose a voice"},
    "step_knowledge": {"ja": "[5/7] 知識フォルダ（任意）", "en": "[5/7] Knowledge folder (optional)"},
    "step_model": {"ja": "[6/7] モデル", "en": "[6/7] Model"},
    "step_start": {"ja": "[7/7] 常駐の設定をして起動します…",
                   "en": "[7/7] Setting up the background service and starting it…"},
    "started": {"ja": "起動しました（今: {status}）。", "en": "Started (now: {status})."},
    "talk_now": {"ja": "話しかけてみてください。", "en": "Go ahead, say something."},
    "start_pending": {"ja": "起動中です。少し待ってからメニューバーのアイコンを見てください。",
                      "en": "Starting up. Give it a moment, then check the menu bar icon."},
    "config_file": {"ja": "設定ファイル: {path}", "en": "Settings file: {path}"},
    "interrupted_resume": {"ja": "\n中断しました。もう一度 `zundamon-jarvis setup` を実行すると、続きから設定できます。",
                           "en": "\nStopped. Run `zundamon-jarvis setup` again to pick up where you left off."},
    "interrupted": {"ja": "\n中断しました。", "en": "\nStopped."},
    # `zundamon-jarvis uninstall`
    "uninstall_stopping": {"ja": "常駐を止めて、自動起動の設定を外します…",
                           "en": "Stopping the background service and removing it from startup…"},
    "uninstall_removed": {"ja": "外しました。", "en": "Done."},
    "uninstall_engine_ask": {"ja": "VOICEVOX エンジン（{path}）も消しますか？ [y/N]: ",
                             "en": "Delete the VOICEVOX engine ({path}) too? [y/N]: "},
    "uninstall_engine_done": {"ja": "エンジンを消しました。", "en": "Engine deleted."},
    "uninstall_config_ask": {"ja": "設定（{path}）も消しますか？ [y/N]: ",
                             "en": "Delete the settings ({path}) too? [y/N]: "},
    "uninstall_config_done": {"ja": "設定を消しました。", "en": "Settings deleted."},
    "uninstall_rest": {"ja": "本体は `uv tool uninstall zundamon-jarvis` で外せます。"
                             "会話のログは {logs} に残っています（14 日で消えます）。",
                       "en": "To remove the app itself, run `uv tool uninstall zundamon-jarvis`. "
                             "Conversation logs stay in {logs} (deleted after 14 days)."},
    "uninstall_model": {"ja": "文字起こしのモデル（数 GB）は {path} にあります。"
                              "いらなければ、そのフォルダを消してください。",
                        "en": "The speech-to-text model (several GB) is in {path}. "
                              "Delete that folder if you don't need it."},
}


def t(key: str, lang: str | None = None, **fields: object) -> str:
    return i18n.text(TEXT, key, lang, **fields)


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
                       url: str = DEFAULT_URL, wait_s: float = ENGINE_WAIT_S,
                       lang: str | None = None) -> AsyncIterator[Any]:
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
            say(t("engine_starting", lang))
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


def headphones_note(sys_: str, lang: str | None = None) -> str:
    return t({"mac": "headphones_mac", "windows": "headphones_windows"}.get(sys_, "headphones_linux"), lang)


def mic_problem_note(sys_: str, problem: str, lang: str | None = None) -> str:
    """What to do when the mic could not be opened (``problem`` "open") or read only silence
    ("silent"). macOS: the grant belongs to the app setup runs under (the terminal)."""
    if sys_ == "mac":
        return t("terminal_mic_note", lang)
    if sys_ == "windows":
        return t("windows_mic_note", lang)
    return t("mic_check_connected" if problem == "open" else "mic_check_muted", lang)


def read_config(path: Path, lang: str | None = None) -> tuple[Config | None, str | None]:
    """The config, or (None, what to tell the user) when ``config.toml`` cannot be parsed."""
    try:
        return load_config(path), None
    except ValueError as e:  # a broken config.toml (TOMLDecodeError is a ValueError)
        return None, t("config_unreadable", lang, path=_tilde(path), error=e)


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


def start_failed_message(e: BaseException, lang: str | None = None) -> str:
    return t("start_failed", lang, error=e)


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


def daemon_mic_text(sys_: str, what: str, lang: str | None = None) -> str:
    """What to say for ``what``: "ask" (before listening) or a ``probe_daemon_mic`` verdict."""
    win = sys_ == "windows"
    key = {"ask": "daemon_mic_ask_windows" if win else "daemon_mic_ask_mac",
           "ok": "daemon_mic_ok",
           "unknown": "daemon_mic_unknown_windows" if win else "daemon_mic_unknown_mac",
           "silent": "windows_daemon_mic_note" if win else "daemon_mic_note"}[what]
    return t(key, lang, seconds=DAEMON_MIC_CHECK_S) if what == "ask" else t(key, lang)


# ---- the wizard ----

async def _ask(con: Console, prompt: str) -> str:
    # Called on the loop thread on purpose: Ctrl-C then interrupts input() at once (a worker
    # thread would keep asyncio.run waiting for Enter). Nothing else runs while asking.
    return con.ask(prompt).strip()


def _voice_label(styles: list[tuple[str, str, int]], speaker_id: int | None, lang: str | None = None) -> str:
    if speaker_id is None:
        return t("voice_default", lang, speaker=DEFAULT_SPEAKER)
    return next((f"{n} {s}" for n, s, sid in styles if sid == speaker_id), t("voice_number", lang, id=speaker_id))


async def _voice_step(con: Console, cfg: Config, path: Path, engine_cm, play, lang: str) -> None:
    async with engine_cm(con.say) as vv:
        speakers = None
        if vv is not None:
            try:
                speakers = await vv.speakers()
            except Exception:
                log.debug("VOICEVOX /speakers failed", exc_info=True)
        if speakers is None:
            con.say(t("voice_skip", lang))
            return
        styles = talk_styles(speakers)
        choices = voice_choices(styles)
        current = _voice_label(styles, cfg.speaker_id, lang)
        while True:
            con.say(t("voice_current", lang, voice=current))
            for i, (label, _) in enumerate(choices, 1):
                con.say(f"  {i}) {label}")
            con.say(t("voice_keep", lang))
            ans = await _ask(con, t("voice_ask", lang))
            if ans in ("", "0"):
                return
            if not (ans.isdigit() and 1 <= int(ans) <= len(choices)):
                con.say(t("voice_bad", lang, n=len(choices)))
                continue
            label, sid = choices[int(ans) - 1]
            try:
                await play(await vv.synth(SAMPLE, sid, speed=cfg.speed))
            except Exception as e:
                con.say(t("preview_failed", lang, error=e))
            if (await _ask(con, t("voice_confirm", lang, voice=label))).lower() in YES:
                save_voice(sid, cfg, path)
                con.say(t("voice_saved", lang, voice=label))
                return


def clean_path_input(raw: str) -> str:
    """A path as typed or dragged into Terminal: surrounding quotes removed, or (unquoted)
    backslash escapes such as ``\\ `` undone."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return re.sub(r"\\(.)", r"\1", s)


async def _knowledge_step(con: Console, cfg: Config, path: Path, lang: str) -> None:
    con.say(t("knowledge_about", lang))
    cur = cfg.knowledge_dir
    prompt = t("knowledge_ask_current", lang, path=_tilde(cur)) if cur else t("knowledge_ask", lang)
    while True:
        ans = await _ask(con, prompt)
        if not ans:
            return
        if ans == "-" and cur is not None:
            save_knowledge_dir(None, cfg, path)
            con.say(t("knowledge_removed", lang))
            return
        p = resolve_folder(ans)
        if p is None:
            con.say(t("folder_not_found_retry", lang, path=ans))
            continue
        save_knowledge_dir(p, cfg, path)
        con.say(t("knowledge_set", lang, path=_tilde(p)))
        return


async def _model_step(con: Console, cfg: Config, path: Path, lang: str) -> None:
    cur = cfg.model or t("model_default", lang)
    models = " / ".join(MODELS)
    while True:
        ans = (await _ask(con, t("model_ask", lang, models=models, current=cur))).lower()
        model = ans or cfg.model
        if model is None:
            return  # an explicit "use the CLI default" stays as it is
        if model in MODELS:
            save_model(model, cfg, path)
            con.say(t("model_set", lang, model=model))
            return
        con.say(t("model_bad", lang, models=models))


async def _mic_step(con: Console, cfg: Config, sys_: str, mic_peak, lang: str) -> None:
    peak = await mic_peak(cfg.mic_device)
    if (peak is None or peak <= 0.0) and sys_ == "mac":
        # The first open brings up the permission dialog; until "許可" is pressed it reads zeros.
        con.say(t("mic_allow_hint", lang))
        if (await _ask(con, t("mic_allow_ask", lang))).lower() != "s":
            peak = await mic_peak(cfg.mic_device)
    if peak is None:
        con.say(t("mic_open_failed", lang))
        con.say(mic_problem_note(sys_, "open", lang))
    elif peak <= 0.0:
        con.say(t("mic_silent", lang))
        con.say(mic_problem_note(sys_, "silent", lang))
    else:
        con.say(t("mic_ok", lang, peak=f"{peak:.3f}"))


async def _daemon_mic_check(con: Console, request, sleep, sys_: str = "mac", lang: str | None = None) -> None:
    """macOS / Windows: the daemon is a different program with its own mic grant."""
    con.say(daemon_mic_text(sys_, "ask", lang))
    for attempt in range(2):
        verdict = await probe_daemon_mic(request, sleep, sys_)
        con.say(daemon_mic_text(sys_, verdict, lang))
        if verdict != "silent":
            return
        if attempt or (await _ask(con, t("daemon_mic_retry", lang))).lower() == "s":
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
    lang: str | None = None,
) -> int:
    """The wizard; ``lang`` is the display language (default: config.toml's, else the OS's)."""
    path = config_path or config_mod.CONFIG_PATH
    sys_ = system or platform.system()
    cfg, problem = read_config(path, lang or i18n.language())
    if cfg is None:
        con.say(problem)
        return 1
    lang = lang or cfg.language or i18n.language()
    engine_cm = voice_engine or (lambda say: open_voice_engine(say, sys_, lang=lang))
    start = start_services or (lambda: install_and_start(sys_))

    con.say(t("step_login", lang))
    login = await login_check(cfg.model)
    if login == "login":
        con.say(t("login_help", lang))
        return EXIT_LOGIN
    con.say(t("limit_note" if login == "limit" else "claude_ok", lang))

    paused = pause_daemon(request)
    started = False
    try:
        con.say("")
        con.say(t("step_mic", lang))
        await _mic_step(con, cfg, sys_, mic_peak, lang)

        con.say("")
        con.say(t("step_headphones", lang, note=headphones_note(sys_, lang)))

        con.say("")
        con.say(t("step_voice", lang))
        await _voice_step(con, cfg, path, engine_cm, play, lang)

        con.say("")
        con.say(t("step_knowledge", lang))
        await _knowledge_step(con, cfg, path, lang)

        con.say("")
        con.say(t("step_model", lang))
        await _model_step(con, cfg, path, lang)

        con.say("")
        con.say(t("step_start", lang))
        try:
            start()
        except START_ERRORS as e:
            con.say(start_failed_message(e, lang))
            return 1
        started = True
        status = await wait_for_status(request, sleep)
        if status and "text" in status:
            con.say(t("started", lang, status=status_label(status, lang)))
            if sys_ in DAEMON_MIC_SYSTEMS:
                await _daemon_mic_check(con, request, sleep, sys_, lang)
            con.say(t("talk_now", lang))
        else:
            con.say(t("start_pending", lang))
        con.say(t("config_file", lang, path=_tilde(path)))
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
        print(t("interrupted_resume"))
        return 130


# ---- `zundamon-jarvis uninstall` ----

def run_uninstall(
    con: Console,
    *,
    system: str | None = None,
    uninstall: Callable[[str], None] | None = None,
    engine_dir: Path | None = None,
    config_path: Path | None = None,
    lang: str | None = None,
) -> int:
    from zundamon_jarvis import engine, service

    sys_ = system or platform.system()
    con.say(t("uninstall_stopping", lang))
    (uninstall or service.uninstall_services)(sys_)
    con.say(t("uninstall_removed", lang))
    engine_dir = engine_dir or engine.default_dest(sys_)
    if engine_dir.is_symlink() or engine_dir.exists():
        if con.ask(t("uninstall_engine_ask", lang, path=_tilde(engine_dir))).strip().lower() in YES[1:]:
            if engine_dir.is_symlink():
                engine_dir.unlink()
            else:
                shutil.rmtree(engine_dir)
            con.say(t("uninstall_engine_done", lang))
    config_path = config_path or platform.config_dir(sys_) / "config.toml"
    if config_path.exists():
        if con.ask(t("uninstall_config_ask", lang, path=_tilde(config_path))).strip().lower() in YES[1:]:
            config_path.unlink()
            con.say(t("uninstall_config_done", lang))
    from zundamon_jarvis.stt import model_cache_dir

    con.say(t("uninstall_rest", lang, logs=_tilde(platform.log_dir(sys_))))
    con.say(t("uninstall_model", lang, path=_tilde(model_cache_dir(sys_))))
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis uninstall` (dispatched by zundamon_jarvis.cli)."""
    if argv:
        print("usage: zundamon-jarvis uninstall", file=sys.stderr)
        return 2
    try:
        return run_uninstall(Console())
    except (KeyboardInterrupt, EOFError):
        print(t("interrupted"))
        return 130
