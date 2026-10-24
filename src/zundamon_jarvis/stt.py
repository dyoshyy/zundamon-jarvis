"""Local Japanese speech-to-text: faster-whisper on CUDA (Linux), mlx-whisper on Apple Silicon (Mac)."""

import ctypes
import glob
import os
import sys
import sysconfig
from pathlib import Path

import numpy as np

FASTER_WHISPER_MODEL = "large-v3-turbo"
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_WHISPER_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"  # what faster-whisper fetches


def _preload_cuda_libs() -> None:
    """Load pip-installed cuBLAS/cuDNN so LD_LIBRARY_PATH is not needed (Linux only)."""
    if sys.platform != "linux":
        return
    root = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    for pkg, names in (
        ("cublas", ("libcublasLt.so.12", "libcublas.so.12")),
        ("cudnn", ("libcudnn.so.9",)),
    ):
        for name in names:
            for path in glob.glob(os.path.join(root, pkg, "lib", name)):
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)


_preload_cuda_libs()


class Transcriber:
    def __init__(
        self,
        model: str = FASTER_WHISPER_MODEL,
        device: str = "cuda",
        compute_type: str = "float16",
    ) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model, device=device, compute_type=compute_type)

    def transcribe(self, audio: np.ndarray) -> str:
        """audio: 16 kHz mono float32."""
        segments, _ = self._model.transcribe(
            audio,
            language="ja",
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return "".join(s.text for s in segments).strip()


class MlxTranscriber:
    def __init__(self, model: str = MLX_MODEL) -> None:
        import mlx_whisper

        self._mlx = mlx_whisper
        self._model = model
        self.transcribe(np.zeros(16000, np.float32))  # load/warm the model once

    def transcribe(self, audio: np.ndarray) -> str:
        """audio: 16 kHz mono float32."""
        result = self._mlx.transcribe(
            np.asarray(audio, dtype=np.float32),
            path_or_hf_repo=self._model,
            language="ja",
            condition_on_previous_text=False,
        )
        return result["text"].strip()


def make_transcriber(system: str | None = None):
    from zundamon_jarvis.platform import system as _system

    if (system or _system()) == "mac":
        return MlxTranscriber()
    return Transcriber()


def download_model(system: str | None = None) -> str:
    """Fetch the speech model into the Hugging Face cache (no GPU, nothing loaded); returns its path."""
    from zundamon_jarvis.platform import system as _system

    if (system or _system()) == "mac":
        from huggingface_hub import snapshot_download

        return snapshot_download(MLX_MODEL)
    from faster_whisper import download_model as fw_download

    return fw_download(FASTER_WHISPER_MODEL)


def model_cache_dir(system: str | None = None) -> Path:
    """Where ``download_model`` puts the model (the Hugging Face hub cache layout)."""
    from zundamon_jarvis.platform import system as _system

    env = os.environ
    hub = (Path(env["HF_HUB_CACHE"]) if env.get("HF_HUB_CACHE")
           else Path(env["HF_HOME"]) / "hub" if env.get("HF_HOME")
           else Path.home() / ".cache" / "huggingface" / "hub")
    repo = MLX_MODEL if (system or _system()) == "mac" else FASTER_WHISPER_REPO
    return hub / ("models--" + repo.replace("/", "--"))
