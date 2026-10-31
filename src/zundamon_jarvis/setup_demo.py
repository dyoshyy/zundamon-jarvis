"""`zundamon-jarvis setup --demo`: the setup page with fakes behind everything that changes the
machine, for looking at it (design checks, screenshots, another OS's texts) without installing
or downloading anything and without touching a running zundamon-jarvis.

Fake: the downloads (simulated progress), the Claude login check, starting the services and the
daemon's control socket, and the config file (a temporary one). Real: the folder dialog, and
VOICEVOX when one already answers on 127.0.0.1:50021 (else fake voices). The microphone is the
real one when the demo shows this OS, else a fake signal. The voice samples are not played
unless ``ZJ_DEMO_PLAY=1`` (a running daemon would hear them).

Environment knobs: ``ZJ_DEMO_SYSTEM`` = mac | windows | linux (whose texts to show),
``ZJ_DEMO_LOGIN`` = ok | limit | login, ``ZJ_DEMO_FAIL`` = engine | model | start (fails once),
``ZJ_DEMO_DAEMON_MIC`` = ok | silent, ``ZJ_DEMO_FAST=1`` (downloads done at once),
``ZJ_DEMO_PORT`` (a fixed port), ``ZJ_DEMO_PLAY=1``, ``ZJ_DEMO_MIC=fake``,
``ZJ_DEMO_LANG`` = ja | en (the page's first language; else the usual: config.toml's, the OS's).
"""

import asyncio
import contextlib
import math
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

from zundamon_jarvis import i18n, platform
from zundamon_jarvis import setup_wizard as wiz
from zundamon_jarvis.mics import MicSource
from zundamon_jarvis.setup_gui import Deps, default_deps, run_gui
from zundamon_jarvis.tts import DEFAULT_URL, RATE

ENGINE_BYTES = 1_936_066_614
MODEL_FILES = (2_400, 340, 2_710_000, 1_620_000_000, 1_070_000)
TEXT = {
    "failed": {"ja": "（デモ）{what} がわざと失敗しました。もう一度試すと通ります",
               "en": "(demo) {what} failed on purpose. Try again and it works"},
    "engine": {"ja": "（デモ）VOICEVOX を起動しています…", "en": "(demo) Starting VOICEVOX…"},
    "no_daemon": {"ja": "（デモ）常駐は動いていません", "en": "(demo) the background service isn't running"},
    "builtin": {"ja": "内蔵マイク", "en": "Built-in mic"},
    "usb": {"ja": "USB マイク", "en": "USB mic"},
    "bad_system": {"ja": "ZJ_DEMO_SYSTEM は mac / windows / linux のどれかです",
                   "en": "ZJ_DEMO_SYSTEM is one of mac / windows / linux"},
    "intro": {"ja": "デモです（{system} の画面）。何もインストールしません。設定は {path} に書きます。",
              "en": "Demo (the {system} screens). Nothing gets installed. Settings go to {path}."},
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().lower()


class _FailOnce:
    def __init__(self, t) -> None:
        self.t = t
        self.failed: set[str] = set()

    def __call__(self, what: str) -> None:
        if _env("ZJ_DEMO_FAIL") == what and what not in self.failed:
            self.failed.add(what)
            raise OSError(self.t("failed", what=what))


def _simulate(total: int, seconds: float, report) -> None:
    steps = max(1, int(seconds / 0.1))
    for i in range(1, steps + 1):
        time.sleep(seconds / steps)
        report(total * i // steps)


class FakeVoicevox:
    """Speakers like the engine's, and a short tone for every sample."""

    async def speakers(self) -> list[dict]:
        return [{"name": name, "styles": [{"name": style, "id": i}]}
                for i, (name, style) in enumerate(wiz.VOICES, start=1)]

    async def synth(self, text: str, speaker_id: int | None = None, *, speed: float | None = None):
        n = int(RATE * 0.8 / (speed or 1.0))
        t = np.arange(n) / RATE
        return (0.15 * np.sin(2 * np.pi * (330 + 40 * (speaker_id or 0)) * t)).astype(np.float32)


class FakeMic:
    """A voice-like signal: louder and quieter every second or so."""

    fallback = False

    def __init__(self, device: Any = None, **_kw: Any) -> None:
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    async def frames(self) -> AsyncIterator[np.ndarray]:
        t = 0.0
        rng = np.random.default_rng()
        while self._open:
            await asyncio.sleep(0.032)
            t += 0.032
            amp = 0.35 * abs(math.sin(t * 1.7)) ** 3
            yield rng.uniform(-amp, amp, 512).astype(np.float32)


def demo_deps(sys_: str, config_path: Path | None = None) -> Deps:
    """``config_path``: the demo's config, whose language (switched on the page) the fakes use."""
    real = default_deps(sys_)

    def t(key: str, **fields: object) -> str:
        return i18n.text(TEXT, key, i18n.language(config_path), **fields)

    fail = _FailOnce(t)
    fast = _env("ZJ_DEMO_FAST") == "1"
    services = {"started": False}

    async def login_check(model: str | None) -> str:
        await asyncio.sleep(1.5)
        return _env("ZJ_DEMO_LOGIN", "ok") or "ok"

    def install_engine(progress) -> None:
        fail("engine")
        _simulate(ENGINE_BYTES, 0.3 if fast else 8, lambda d: progress("download", d, ENGINE_BYTES))
        progress("extract", 0, 0)
        time.sleep(0.1 if fast else 2)
        progress("check", 0, 0)
        time.sleep(0.1 if fast else 1)

    def download_model(progress) -> None:
        fail("model")
        done, total = 0, 0
        for size in MODEL_FILES:  # totals grow as each file starts, like huggingface_hub's
            total += size
            base = done
            _simulate(size, 0.05 if fast else max(0.3, 9 * size / sum(MODEL_FILES)),
                      lambda d, base=base, total=total: progress(base + d, total))
            done += size

    @contextlib.asynccontextmanager
    async def voice_engine(say) -> AsyncIterator[Any]:
        from zundamon_jarvis.tts import Voicevox

        vv = Voicevox(DEFAULT_URL)
        try:
            await vv.version()
            running = True
        except Exception:
            running = False
        if not running:  # none here: pretend to start one
            await vv.aclose()
            say(t("engine"))
            await asyncio.sleep(1.0)
            yield FakeVoicevox()
            return
        try:
            yield vv
        finally:
            await vv.aclose()

    async def play(pcm: Any) -> None:
        if _env("ZJ_DEMO_PLAY") == "1":
            await wiz.play_pcm(pcm)
        else:
            await asyncio.sleep(len(pcm) / RATE)

    def start_services() -> None:
        fail("start")
        time.sleep(2)
        services["started"] = True

    def request(cmd: str, timeout: float = 3.0) -> dict:
        if not services["started"]:
            raise FileNotFoundError(t("no_daemon"))
        loud = 0.0 if _env("ZJ_DEMO_DAEMON_MIC") == "silent" else 0.04
        status = {"state": "listening", "text": "Listening"}
        if cmd == "debug":
            return {**status, "debug": {"paused": False, "frames_last_5s": 156, "max_abs_last_5s": loud}}
        return status

    this_os = sys_ == platform.system() and _env("ZJ_DEMO_MIC") != "fake"
    fake_sources = [MicSource("builtin", t("builtin")), MicSource("usb", t("usb"))]
    return Deps(
        login_check=login_check,
        voice_engine=voice_engine,
        play=play,
        start_services=start_services,
        request=request,
        audio_factory=real.audio_factory if this_os else FakeMic,
        list_mics=real.list_mics if this_os else (lambda: fake_sources),
        default_mic=real.default_mic if this_os else (lambda: "builtin"),
        engine_installed=lambda: False,
        install_engine=install_engine,
        model_installed=lambda: False,
        download_model=download_model,
        pick_folder=real.pick_folder,
    )


def run_demo() -> int:
    sys_ = _env("ZJ_DEMO_SYSTEM") or platform.system()
    if sys_ not in ("mac", "windows", "linux"):
        print(i18n.text(TEXT, "bad_system"))
        return 2
    path = Path(tempfile.mkdtemp(prefix="zundamon-jarvis-setup-demo-")) / "config.toml"
    lang = _env("ZJ_DEMO_LANG")  # written down, so the demo's own fakes can follow the page's switch
    path.write_text(f'language = "{lang if lang in i18n.LANGUAGES else i18n.language()}"\n', encoding="utf-8")
    print(i18n.text(TEXT, "intro", i18n.language(path), system=sys_, path=path))
    port = int(os.environ.get("ZJ_DEMO_PORT") or 0)
    return run_gui(demo_deps(sys_, path), config_path=path, system=sys_, port=port)
