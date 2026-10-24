import sys
import types

import numpy as np

from zundamon_jarvis import stt


def test_make_transcriber_picks_backend(monkeypatch):
    class FakeLinux:
        pass

    class FakeMac:
        pass

    monkeypatch.setattr(stt, "Transcriber", FakeLinux)
    monkeypatch.setattr(stt, "MlxTranscriber", FakeMac)
    assert isinstance(stt.make_transcriber("mac"), FakeMac)
    assert isinstance(stt.make_transcriber("linux"), FakeLinux)


def test_mlx_transcriber_calls_mlx_with_japanese(monkeypatch):
    calls = []

    def fake_transcribe(audio, **kw):
        calls.append((audio, kw))
        return {"text": " こんにちは "}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe))
    t = stt.MlxTranscriber()
    out = t.transcribe(np.zeros(1600, np.float64))
    assert out == "こんにちは"
    audio, kw = calls[-1]
    assert audio.dtype == np.float32
    assert kw["language"] == "ja"
    assert kw["condition_on_previous_text"] is False
    assert kw["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
