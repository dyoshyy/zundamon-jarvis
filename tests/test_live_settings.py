"""Daemon-side settings: validation, live effects, persistence, preview gating."""

import asyncio

import httpx
import numpy as np
import pytest
from fakes import settle

from zundamon_jarvis.config import Config, load_config
from zundamon_jarvis.daemon import TtsWatch
from zundamon_jarvis.live_settings import LiveSettings, parse_model, parse_speaker, parse_speed

SPEAKERS = [
    {"name": "ずんだもん", "styles": [{"name": "ノーマル", "id": 3}, {"name": "あまあま", "id": 1}]},
    {"name": "青山龍星", "styles": [{"name": "ノーマル", "id": 13}]},
]


class FakeVV:
    def __init__(self):
        self.speaker_id = 13
        self.speed = 1.2
        self.up = True
        self.synths = []

    async def speakers(self):
        if not self.up:
            raise httpx.ConnectError("refused")
        return SPEAKERS

    async def find_speaker(self, name, style="ノーマル"):
        return 13

    async def version(self):
        return "0.26"

    async def synth(self, text, speaker_id=None, *, speed=None):
        if not self.up:
            raise httpx.ConnectError("refused")
        self.synths.append((text, speaker_id, speed))
        return np.zeros(10, np.float32)


class FakeOrch:
    def __init__(self):
        self.user_talking = False
        self.saying = False
        self.has_session = False
        self.restart = "none"

    def restart_session(self):
        return self.restart


class Ears:
    def __init__(self):
        self.calls = []

    def __call__(self, muted):
        self.calls.append(muted)


class PreviewPlayer:
    def __init__(self):
        self.played = []
        self.stops = 0
        self.gate: asyncio.Event | None = None
        self._stopped: asyncio.Event | None = None

    async def play(self, pcm):
        self.played.append(len(pcm))
        self._stopped = asyncio.Event()
        if self.gate is not None:
            done = asyncio.ensure_future(self.gate.wait())
            stop = asyncio.ensure_future(self._stopped.wait())
            await asyncio.wait({done, stop}, return_when=asyncio.FIRST_COMPLETED)
            done.cancel()
            stop.cancel()
            return self.gate.is_set()
        return True

    def stop(self):
        self.stops += 1
        if self._stopped is not None:
            self._stopped.set()


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('mic_device = "x"\nunknown = 1\n')
    cfg = Config(speed=1.2, model="sonnet")
    vv = FakeVV()
    watch = TtsWatch(vv, lambda _: None)
    orch = FakeOrch()
    ears = Ears()
    player = PreviewPlayer()
    live = LiveSettings(cfg, vv, watch, orch, path=path, play=player.play,
                        stop_playback=player.stop, mute_ears=ears)
    return live, cfg, vv, watch, orch, ears, player, path


# ---- parsing ----

@pytest.mark.parametrize("raw, want", [("0.8", 0.8), ("1.2", 1.2), ("2", 2.0), (" 1.25 ", 1.25)])
def test_parse_speed_accepts_the_range(raw, want):
    assert parse_speed(raw) == want


@pytest.mark.parametrize("raw", ["0.79", "2.01", "nan", "inf", "-1", "", "fast", "1e400", "1,2"])
def test_parse_speed_rejects(raw):
    with pytest.raises(ValueError):
        parse_speed(raw)


@pytest.mark.parametrize("raw", ["-1", "1.5", "x", "", "99999999", "+3", "0x10"])
def test_parse_speaker_rejects(raw):
    with pytest.raises(ValueError):
        parse_speaker(raw)


def test_parse_speaker_and_model():
    assert parse_speaker("13") == 13
    assert parse_speaker("0") == 0
    assert parse_model("opus") == "opus"
    for bad in ("gpt", "", "Opus", "claude-opus-4"):
        with pytest.raises(ValueError):
            parse_model(bad)


# ---- get / set ----

def test_snapshot(env):
    live, *_ = env
    assert live.snapshot() == {"speed": 1.2, "speaker_id": 13, "model": "sonnet",
                               "session_model": None, "models": ["sonnet", "opus", "haiku"],
                               "mic_device": None, "mic_active": None,
                               "speed_min": 0.8, "speed_max": 2.0}


async def test_set_speed_applies_live_and_persists_keeping_other_keys(env):
    live, cfg, vv, *_, path = env
    resp = await live.set("speed", "1.5")
    assert resp["settings"]["speed"] == 1.5 and resp["saved"] is True
    assert vv.speed == 1.5 and cfg.speed == 1.5
    saved = load_config(path)
    assert saved.speed == 1.5 and saved.mic_device == "x"
    assert "unknown = 1" in path.read_text()


async def test_set_speaker_overrides_the_default_resolution(env):
    live, cfg, vv, _, *rest = env
    path = rest[-1]
    watch = TtsWatch(vv, lambda _: None, speaker_name="青山龍星")  # not resolved yet
    live._watch = watch
    resp = await live.set("speaker", "3")
    assert "error" not in resp
    assert vv.speaker_id == 3 and cfg.speaker_id == 3
    await watch.check()  # the 青山龍星 lookup must not win afterwards
    assert vv.speaker_id == 3 and watch.resolved
    assert load_config(path).speaker_id == 3


async def test_set_speaker_rejects_unknown_ids_and_an_unreachable_engine(env):
    live, cfg, vv, *_, path = env
    assert "error" in await live.set("speaker", "999")
    vv.up = False
    assert "error" in await live.set("speaker", "3")
    assert vv.speaker_id == 13 and load_config(path).speaker_id is None


async def test_set_model_takes_effect_for_the_next_session(env):
    live, cfg, *_, path = env
    resp = await live.set("model", "opus")
    assert cfg.model == "opus" and resp["settings"]["model"] == "opus"
    assert load_config(path).model == "opus"


async def test_set_rejects_bad_input_and_changes_nothing(env):
    live, cfg, vv, *_, path = env
    before = path.read_text()
    for key, raw in [("speed", "3"), ("speed", "x"), ("model", "gpt"), ("volume", "1"),
                     ("speaker", "-2")]:
        resp = await live.set(key, raw)
        assert "error" in resp, (key, raw)
    assert (cfg.speed, cfg.model, vv.speed) == (1.2, "sonnet", 1.2)
    assert path.read_text() == before


async def test_a_broken_config_file_still_applies_live(env):
    live, cfg, vv, *_, path = env
    path.write_text("= broken\n")
    resp = await live.set("speed", "1.6")
    assert vv.speed == 1.6 and resp["saved"] is False
    assert path.read_text() == "= broken\n"


def test_session_model_is_reported_while_a_session_is_open(env):
    live, cfg, vv, watch, orch, *_ = env
    live.brain_started("sonnet")
    assert live.snapshot()["session_model"] is None  # no session open
    orch.has_session = True
    assert live.snapshot()["session_model"] == "sonnet"


def test_new_session_passes_the_orchestrator_answer(env):
    live, cfg, vv, watch, orch, *_ = env
    for answer in ("closed", "none", "busy"):
        orch.restart = answer
        assert live.new_session()["session"] == answer


# ---- preview ----

async def test_preview_synthesizes_the_given_voice_and_speed_and_plays_it(env):
    live, cfg, vv, watch, orch, ears, player, _ = env
    resp = await live.preview("3", "1.8", "この声で話すね。")
    assert resp == {"ok": True}
    await settle()
    assert vv.synths == [("この声で話すね。", 3, 1.8)]
    assert player.played == [10]
    assert ears.calls == [True, False]  # the mic is ignored while the sample plays
    assert (vv.speaker_id, vv.speed) == (13, 1.2)  # a preview changes nothing


@pytest.mark.parametrize("flag, reason", [("user_talking", "user_speaking"), ("saying", "speaking")])
async def test_preview_never_plays_over_the_user_or_a_reply(env, flag, reason):
    live, cfg, vv, watch, orch, ears, player, _ = env
    setattr(orch, flag, True)
    resp = await live.preview("3", "1.2", "やあ")
    assert resp["ok"] is False and resp["busy"] == reason
    await settle()
    assert vv.synths == [] and player.played == [] and ears.calls == []


async def test_preview_validates_its_input(env):
    live, *_ = env
    for args in [("x", "1.2", "やあ"), ("3", "9", "やあ"), ("3", "1.2", ""),
                 ("3", "1.2", "あ" * 201), ("3", "1.2", "a\x07b")]:
        assert "error" in await live.preview(*args), args


async def test_preview_reports_an_unreachable_engine(env):
    live, cfg, vv, *_ = env
    vv.up = False
    resp = await live.preview("3", "1.2", "やあ")
    assert "error" in resp


async def test_a_new_preview_replaces_the_one_playing(env):
    live, cfg, vv, watch, orch, ears, player, _ = env
    player.gate = asyncio.Event()
    await live.preview("3", "1.2", "一つ目")
    await settle()
    assert player.played == [10]
    await live.preview("1", "1.2", "二つ目")
    await settle()
    assert player.stops >= 1 and len(player.played) == 2
    player.gate.set()
    await settle()
    assert ears.calls[-1] is False and not live.previewing


async def test_zundamon_jarvis_starting_to_speak_cuts_the_preview(env):
    live, cfg, vv, watch, orch, ears, player, _ = env
    player.gate = asyncio.Event()
    await live.preview("3", "1.2", "やあ")
    await settle()
    assert live.previewing
    live.stop_preview()
    await settle()
    assert player.stops == 1 and not live.previewing and ears.calls == [True, False]


async def test_close_cancels_a_playing_preview(env):
    live, cfg, vv, watch, orch, ears, player, _ = env
    player.gate = asyncio.Event()
    await live.preview("3", "1.2", "やあ")
    await settle()
    await live.aclose()
    assert not live.previewing and ears.calls == [True, False]


async def test_set_mic_saves_but_the_running_mic_stays(env):
    live, cfg, *_, path = env
    r = await live.set("mic", "zundamon_jarvis_aec_source")
    assert r["ok"] and r["saved"]
    assert (r["settings"]["mic_device"], r["settings"]["mic_active"]) == ("zundamon_jarvis_aec_source", None)
    assert load_config(path).mic_device == "zundamon_jarvis_aec_source"
    r = await live.set("mic", "default")  # follow the OS default: the line is removed
    assert r["settings"]["mic_device"] is None
    assert "mic_device" not in path.read_text() and "unknown = 1" in path.read_text()
    assert "error" in await live.set("mic", "  ")


# ---- fix round: the ears never stay muted ----

async def test_the_mic_is_muted_before_preview_returns(env):
    live, cfg, vv, watch, orch, ears, player, _ = env
    player.gate = asyncio.Event()
    assert await live.preview("3", "1.2", "やあ") == {"ok": True}
    assert ears.calls == [True]  # synchronously: no frame can slip in before the mute
    player.gate.set()
    await settle()
    assert ears.calls == [True, False]


async def test_a_playback_that_never_finishes_is_cut_and_the_mic_comes_back(tmp_path):
    vv, ears, stops = FakeVV(), Ears(), []

    async def hang(pcm):
        await asyncio.Event().wait()  # the sink never reports the end

    live = LiveSettings(Config(), vv, TtsWatch(vv, lambda _: None), FakeOrch(),
                        path=tmp_path / "c.toml", play=hang, stop_playback=lambda: stops.append(1),
                        mute_ears=ears, rate=1000, play_margin_s=0.05)
    await live.preview("3", "1.2", "やあ")  # 10 samples at 1000 Hz + 0.05 s margin
    await asyncio.sleep(0.2)
    assert ears.calls == [True, False] and stops == [1] and not live.previewing


async def test_a_playback_that_raises_still_unmutes(tmp_path):
    vv, ears = FakeVV(), Ears()

    async def boom(pcm):
        raise RuntimeError("no output device")

    live = LiveSettings(Config(), vv, TtsWatch(vv, lambda _: None), FakeOrch(),
                        path=tmp_path / "c.toml", play=boom, stop_playback=lambda: None,
                        mute_ears=ears)
    assert (await live.preview("3", "1.2", "やあ"))["ok"] is True
    await settle()
    assert ears.calls == [True, False]


async def test_any_synth_failure_is_an_error_reply(env):
    live, cfg, vv, *_ = env

    async def bad(*a, **kw):
        raise KeyError("speedScale")

    vv.synth = bad
    resp = await live.preview("3", "1.2", "やあ")
    assert "error" in resp and not live.previewing


async def test_a_preview_cut_before_it_starts_playing_still_unmutes(env):
    live, cfg, vv, watch, orch, ears, player, _ = env
    await live.preview("3", "1.2", "やあ")
    live.stop_preview()  # ZundamonJarvis starts talking before the play task ever ran
    await settle()
    assert ears.calls == [True, False] and player.played == []
    await live.preview("1", "1.2", "つぎ")  # and the next preview works normally
    await settle()
    assert ears.calls == [True, False, True, False]
