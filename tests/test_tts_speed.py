"""Voicevox client with a mocked HTTP transport (no engine needed)."""

import io
import json
import wave

import httpx
import numpy as np

from zundamon_jarvis.tts import Voicevox


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(np.zeros(240, np.int16).tobytes())
    return buf.getvalue()


def engine(seen):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/audio_query":
            seen.append(("query", dict(req.url.params)))
            return httpx.Response(200, json={"speedScale": 1.0, "pitchScale": 0.0})
        if req.url.path == "/synthesis":
            seen.append(("synthesis", dict(req.url.params), json.loads(req.content)))
            return httpx.Response(200, content=_wav())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_synth_sends_the_speed_as_speed_scale():
    seen = []
    vv = Voicevox(speaker_id=13, speed=1.3, transport=engine(seen))
    try:
        pcm = await vv.synth("やあ")
        assert pcm.shape == (240,)
        assert seen[0] == ("query", {"text": "やあ", "speaker": "13"})
        _, params, body = seen[1]
        assert params == {"speaker": "13"}
        assert body["speedScale"] == 1.3 and body["pitchScale"] == 0.0
    finally:
        await vv.aclose()


async def test_speed_and_speaker_can_be_given_per_call_or_changed_live():
    seen = []
    vv = Voicevox(speaker_id=13, transport=engine(seen))
    try:
        assert vv.speed == 1.0  # the engine's own default unless told otherwise
        await vv.synth("a", 3, speed=1.8)
        assert seen[-1][1] == {"speaker": "3"} and seen[-1][2]["speedScale"] == 1.8
        vv.speed = 1.1
        vv.speaker_id = 8
        await vv.synth("b")
        assert seen[-1][1] == {"speaker": "8"} and seen[-1][2]["speedScale"] == 1.1
    finally:
        await vv.aclose()


def test_talk_styles_skips_singing_and_junk():
    from zundamon_jarvis.tts import talk_styles

    speakers = [
        {"name": "四国めたん", "styles": [{"name": "ノーマル", "id": 2, "type": "streaming_talk"},
                                      {"name": "あまあま", "id": 0}]},
        {"name": "波音リツ", "styles": [{"name": "ノーマル", "id": 9, "type": "talk"},
                                     {"name": "ハミング", "id": 3001, "type": "frame_decode"},
                                     {"name": "歌", "id": 6000, "type": "sing"}]},
        {"name": 5, "styles": []}, "junk", {"name": "x", "styles": [{"id": "7"}, {"id": True}]},
    ]
    assert talk_styles(speakers) == [("四国めたん", "ノーマル", 2), ("四国めたん", "あまあま", 0),
                                     ("波音リツ", "ノーマル", 9)]
    assert talk_styles({"not": "a list"}) == []
