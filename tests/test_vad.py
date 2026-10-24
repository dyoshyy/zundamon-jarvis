import numpy as np
import pytest

from helpers import resample_24k_to_16k
from zundamon_jarvis.tts import Voicevox
from zundamon_jarvis.vad import SileroVad

FRAME = 512


def _frames(audio: np.ndarray):
    n = len(audio) // FRAME
    return audio[: n * FRAME].reshape(n, FRAME)


def test_silence_is_not_speech():
    p = SileroVad().prob(np.zeros(FRAME, np.float32))
    assert 0.0 <= p < 0.5


def test_reset_restores_fresh_state():
    rng = np.random.default_rng(0)
    noisy = (rng.standard_normal((20, FRAME)) * 0.3).astype(np.float32)
    zero = np.zeros(FRAME, np.float32)
    fresh = SileroVad().prob(zero)
    vad = SileroVad()
    for f in noisy:
        vad.prob(f)
    assert vad.prob(zero) != pytest.approx(fresh, abs=1e-6)  # state really was dirty
    vad.reset()
    assert vad.prob(zero) == pytest.approx(fresh, abs=1e-6)


@pytest.mark.integration
async def test_speech_detected():
    vv = Voicevox(speaker_id=13)
    try:
        audio24 = await vv.synth("こんにちは、今日はいい天気ですね。")
    finally:
        await vv.aclose()
    audio = resample_24k_to_16k(audio24)
    vad = SileroVad()
    assert max(vad.prob(f) for f in _frames(audio)) > 0.8


def test_context_is_not_aliased_to_caller_buffer():
    rng = np.random.default_rng(1)
    frames = (rng.standard_normal((2, FRAME)) * 0.3).astype(np.float32)
    ref = SileroVad()
    ref.prob(frames[0].copy())
    expected = ref.prob(frames[1])

    vad = SileroVad()
    buf = frames[0].copy()
    vad.prob(buf)
    buf[:] = 1.0  # audio callback reuses its buffer
    assert vad.prob(frames[1]) == pytest.approx(expected, abs=1e-6)


def test_vad_model_is_packaged():
    import sys
    from importlib import resources

    from zundamon_jarvis import vad

    asset = resources.files("zundamon_jarvis") / "assets" / "silero_vad_v6.onnx"
    assert asset.is_file()
    assert vad._model_path() == str(asset)
    assert "faster_whisper" not in vad._model_path()
