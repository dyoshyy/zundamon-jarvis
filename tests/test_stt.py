import time

import numpy as np
import pytest
from helpers import resample_24k_to_16k

from zundamon_jarvis.stt import Transcriber
from zundamon_jarvis.text import is_hallucination
from zundamon_jarvis.tts import Voicevox

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def transcriber():
    return Transcriber()


async def _speech() -> np.ndarray:
    vv = Voicevox(speaker_id=13)
    try:
        return resample_24k_to_16k(await vv.synth("明日の予定を教えて"))
    finally:
        await vv.aclose()


async def test_transcribes_japanese(transcriber):
    assert "予定" in transcriber.transcribe(await _speech())


def test_silence_yields_nothing_or_hallucination(transcriber):
    text = transcriber.transcribe(np.zeros(16000 * 10, np.float32))
    assert text == "" or is_hallucination(text)


async def test_utterance_latency(transcriber):
    audio = await _speech()
    transcriber.transcribe(audio)  # warm-up
    t0 = time.perf_counter()
    transcriber.transcribe(audio)
    elapsed = time.perf_counter() - t0
    print(f"audio {len(audio) / 16000:.2f}s transcribed in {elapsed:.3f}s")
    assert elapsed < 0.5
