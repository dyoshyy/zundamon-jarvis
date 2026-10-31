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


# ---- Windows: cuBLAS/cuDNN DLL directories, CUDA or the CPU ----

def test_make_transcriber_on_windows_goes_through_the_device_choice(monkeypatch):
    monkeypatch.setattr(stt, "windows_transcriber", lambda: "win")
    assert stt.make_transcriber("windows") == "win"


def _site(tmp_path, *pkgs):
    for pkg in pkgs:
        (tmp_path / "nvidia" / pkg / "bin").mkdir(parents=True)
    return str(tmp_path)


def test_cuda_dll_dirs_go_on_the_dll_path_and_in_front_of_path(tmp_path, monkeypatch):
    monkeypatch.setattr(stt, "_DLL_DIRS", [])
    site = _site(tmp_path, "cublas", "cudnn")
    (tmp_path / "nvidia" / "cuda_nvrtc" / "bin").mkdir(parents=True)  # not ours to add
    added = []
    env = {"PATH": "/windows/system32"}
    dirs = stt.add_cuda_dll_dirs(site, add_dll_directory=lambda d: added.append(d) or d, environ=env)
    want = [str(tmp_path / "nvidia" / "cublas" / "bin"), str(tmp_path / "nvidia" / "cudnn" / "bin")]
    assert dirs == want and added == want and stt._DLL_DIRS == want  # handles kept open
    assert env["PATH"].split(stt.os.pathsep) == want + ["/windows/system32"]
    stt.add_cuda_dll_dirs(site, add_dll_directory=lambda d: d, environ=env)
    assert env["PATH"].split(stt.os.pathsep) == want + ["/windows/system32"]  # not twice


def test_cuda_dll_dirs_without_the_nvidia_wheels_change_nothing(tmp_path):
    env = {"PATH": "x"}
    assert stt.add_cuda_dll_dirs(str(tmp_path), add_dll_directory=lambda d: 1 / 0, environ=env) == []
    assert env == {"PATH": "x"}
    only = _site(tmp_path, "cudnn")

    def refuses(d):
        raise OSError("not a directory")

    env = {}
    assert stt.add_cuda_dll_dirs(only, add_dll_directory=refuses, environ=env) == [
        str(tmp_path / "nvidia" / "cudnn" / "bin")]
    assert env["PATH"] == str(tmp_path / "nvidia" / "cudnn" / "bin")  # PATH still helps


class FakeWhisper:
    made: list = []

    def __init__(self, device, compute_type, fail=False):
        self.device, self.compute_type, self.fail = device, compute_type, fail
        FakeWhisper.made.append((device, compute_type))

    def transcribe(self, audio):
        assert audio.dtype == np.float32 and len(audio) == 16000
        if self.fail:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return ""


def test_windows_uses_cuda_when_it_runs(monkeypatch):
    FakeWhisper.made = []
    t = stt.windows_transcriber(FakeWhisper, cuda_device_count=lambda: 1)
    assert (t.device, t.compute_type) == ("cuda", "float16")
    assert FakeWhisper.made == [("cuda", "float16")]


def test_windows_falls_back_to_the_cpu(caplog):
    caplog.set_level("INFO", logger="zundamon_jarvis.stt")
    FakeWhisper.made = []
    t = stt.windows_transcriber(FakeWhisper, cuda_device_count=lambda: 0)
    assert (t.device, t.compute_type) == ("cpu", "int8") and FakeWhisper.made == [("cpu", "int8")]
    assert "no CUDA device" in caplog.text

    FakeWhisper.made = []

    def broken_cuda(device, compute_type):
        return FakeWhisper(device, compute_type, fail=device == "cuda")

    t = stt.windows_transcriber(broken_cuda, cuda_device_count=lambda: 1)
    assert (t.device, t.compute_type) == ("cpu", "int8")
    assert FakeWhisper.made == [("cuda", "float16"), ("cpu", "int8")]
    assert "cublas64_12.dll" in caplog.text

    def no_driver():
        raise RuntimeError("CUDA driver version is insufficient")

    t = stt.windows_transcriber(FakeWhisper, cuda_device_count=no_driver)
    assert t.device == "cpu"


def test_linux_keeps_cuda_without_asking(monkeypatch):
    made = []
    monkeypatch.setattr(stt, "Transcriber", lambda **kw: made.append(kw) or "t")
    monkeypatch.setattr(stt, "windows_transcriber", lambda: 1 / 0)
    assert stt.make_transcriber("linux") == "t" and made == [{}]  # Transcriber's cuda/float16
