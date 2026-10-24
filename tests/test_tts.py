import pytest

from zundamon_jarvis.tts import Voicevox

pytestmark = pytest.mark.integration


async def test_find_speaker_returns_int():
    vv = Voicevox()
    try:
        assert isinstance(await vv.find_speaker("青山龍星"), int)
    finally:
        await vv.aclose()


async def test_synth_has_enough_audio():
    vv = Voicevox()
    try:
        vv.speaker_id = await vv.find_speaker("青山龍星")
        pcm = await vv.synth("こんにちは")
        assert pcm.dtype.name == "float32"
        assert len(pcm) >= 0.3 * 24000
    finally:
        await vv.aclose()
