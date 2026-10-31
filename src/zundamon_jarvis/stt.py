"""Local Japanese speech-to-text: faster-whisper on CUDA (Linux), mlx-whisper on Apple Silicon
(Mac), faster-whisper on CUDA or else the CPU (Windows).

Windows: the cuBLAS/cuDNN DLLs come from the ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` wheels
(``site-packages/nvidia/*/bin``). CTranslate2 loads them by name at run time, so those
directories go on the DLL search path (``os.add_dll_directory``) and on PATH (a plain
``LoadLibrary``, and cuBLAS loading cuBLASLt, only search PATH) before ctranslate2 is imported.
CTranslate2's Windows wheel ships its own ``cudnn64_9.dll`` (9.10.2.21), which loads the cuDNN
sub-libraries from PATH and refuses ones of another version: pyproject pins ``nvidia-cudnn-cu12``
to that version on Windows. A PC without an NVIDIA GPU (no CUDA device), or one where CUDA
fails to run a first, silent transcription, gets the CPU with int8 weights: slower, but it works.
"""

import ctypes
import glob
import logging
import os
import sys
import sysconfig
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

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


def cuda_dll_dirs(purelib: str) -> list[str]:
    """Windows: the existing ``bin`` directories of the pip-installed cuBLAS and cuDNN."""
    root = os.path.join(purelib, "nvidia")
    return [d for pkg in ("cublas", "cudnn") if os.path.isdir(d := os.path.join(root, pkg, "bin"))]


_DLL_DIRS: list[Any] = []  # add_dll_directory handles: the directory leaves the path when closed


def add_cuda_dll_dirs(purelib: str | None = None,
                      add_dll_directory: Callable[[str], Any] | None = None,
                      environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Windows: put the cuBLAS/cuDNN ``bin`` directories on the DLL search path and in front of
    PATH (once); returns the directories."""
    dirs = cuda_dll_dirs(purelib or sysconfig.get_paths()["purelib"])
    add = add_dll_directory or os.add_dll_directory  # type: ignore[attr-defined]
    path = environ.get("PATH", "")
    for d in dirs:
        try:
            _DLL_DIRS.append(add(d))
        except OSError:
            log.debug("add_dll_directory(%s) failed", d, exc_info=True)
    known = {p.lower() for p in path.split(os.pathsep) if p}
    new = [d for d in dirs if d.lower() not in known]
    if new:
        environ["PATH"] = os.pathsep.join(new + ([path] if path else []))
    return dirs


_preload_cuda_libs()
if sys.platform == "win32":
    add_cuda_dll_dirs()


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


def _cuda_device_count() -> int:
    import ctranslate2

    return ctranslate2.get_cuda_device_count()


def windows_transcriber(factory: Callable[..., Any] = Transcriber,
                        cuda_device_count: Callable[[], int] = _cuda_device_count) -> Any:
    """Windows: CUDA (float16) when there is a CUDA device and it transcribes a second of
    silence (cuBLAS/cuDNN are loaded on the first run, not with the model), else the CPU."""
    try:
        count = cuda_device_count()
    except Exception as e:  # noqa: BLE001 -- no driver, a broken install: the CPU still works
        log.warning("cannot count CUDA devices (%s)", e)
        count = 0
    if count > 0:
        try:
            t = factory(device="cuda", compute_type="float16")
            t.transcribe(np.zeros(16000, np.float32))
            return t
        except Exception as e:  # noqa: BLE001 -- e.g. a cuBLAS/cuDNN DLL that will not load
            log.warning("speech recognition on CUDA failed (%s); using the CPU (int8)", e)
    else:
        log.info("no CUDA device; speech recognition runs on the CPU (int8)")
    return factory(device="cpu", compute_type="int8")


def make_transcriber(system: str | None = None):
    from zundamon_jarvis.platform import system as _system

    sys_ = system or _system()
    if sys_ == "mac":
        return MlxTranscriber()
    if sys_ == "windows":
        return windows_transcriber()
    return Transcriber()


# What faster_whisper.utils.download_model fetches of FASTER_WHISPER_REPO (its allow_patterns):
# fetching the same files ourselves fills the cache WhisperModel(FASTER_WHISPER_MODEL) reads.
FASTER_WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json",
                        "vocabulary.*")


class _NullFile:
    def write(self, _s: str) -> None:
        pass

    def flush(self) -> None:
        pass


def byte_progress_tqdm(report) -> type:
    """A ``tqdm_class`` for ``snapshot_download`` that draws nothing and calls ``report`` with
    the bytes so far. huggingface_hub (1.x and later) makes its byte bars (unit "B": bytes
    received and bytes written, totals grown as each file starts) and a file-count bar from
    this class and updates them from its worker threads; the furthest byte bar is reported.
    An older hub that only counts files with it reports nothing (the page shows "in progress")."""
    import threading

    from tqdm.std import tqdm

    lock = threading.Lock()
    bars: list = []

    class _Bytes(tqdm):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kw) -> None:
            kw.pop("name", None)
            kw["disable"] = False
            kw["file"] = _NullFile()
            super().__init__(*args, **kw)
            if self.unit == "B":
                with lock:
                    bars.append(self)

        def update(self, n: float | None = 1) -> bool | None:
            out = super().update(n)
            if self.unit == "B":
                with lock:
                    total = max((int(b.total or 0) for b in bars), default=0)
                    done = max((int(b.n) for b in bars), default=0)
                report(min(done, total) if total else done, total or None)
            return out

    return _Bytes


def download_model(system: str | None = None, *, progress=None, local_only: bool = False) -> str:
    """Fetch the speech model into the Hugging Face cache (no GPU, nothing loaded); returns its path.

    ``progress(done, total)`` gets the bytes of the whole model as they arrive (the setup page's
    bar; total None until the sizes are known). ``local_only`` only looks in the cache: it
    returns the path when the model is completely there and raises otherwise (the "is it there
    already" check, no network)."""
    from zundamon_jarvis.platform import system as _system
    from huggingface_hub import snapshot_download

    kw: dict = {"local_files_only": local_only}
    if progress is not None:
        kw["tqdm_class"] = byte_progress_tqdm(progress)
    if (system or _system()) == "mac":
        return snapshot_download(MLX_MODEL, **kw)  # what mlx_whisper loads (the whole repo)
    return snapshot_download(FASTER_WHISPER_REPO, allow_patterns=list(FASTER_WHISPER_FILES), **kw)


def model_cache_dir(system: str | None = None) -> Path:
    """Where ``download_model`` puts the model (the Hugging Face hub cache layout)."""
    from zundamon_jarvis.platform import system as _system

    env = os.environ
    hub = (Path(env["HF_HUB_CACHE"]) if env.get("HF_HUB_CACHE")
           else Path(env["HF_HOME"]) / "hub" if env.get("HF_HOME")
           else Path.home() / ".cache" / "huggingface" / "hub")
    repo = MLX_MODEL if (system or _system()) == "mac" else FASTER_WHISPER_REPO
    return hub / ("models--" + repo.replace("/", "--"))
