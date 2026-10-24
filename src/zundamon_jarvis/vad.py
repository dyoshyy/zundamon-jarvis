"""Silero VAD wrapper (onnxruntime + numpy, no torch).

Uses the Silero VAD v6 ONNX model bundled in this package (copied from faster-whisper; see assets/LICENSE-silero). That export is
batched: input is (batch, 64 context + 512 samples) plus LSTM state h/c; we
feed it one row at a time and carry the state and context ourselves.
"""

from importlib import resources

import numpy as np
import onnxruntime as ort

FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
_STATE_SHAPE = (1, 1, 128)


def _model_path() -> str:
    path = resources.files("zundamon_jarvis") / "assets" / "silero_vad_v6.onnx"
    if not path.is_file():
        raise FileNotFoundError(
            f"Silero VAD model not found at {path}; the package asset is missing"
        )
    return str(path)


class SileroVad:
    def __init__(self) -> None:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = ort.InferenceSession(
            _model_path(), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self) -> None:
        """Clear recurrent state and audio context (e.g. after a mic pause)."""
        self._h = np.zeros(_STATE_SHAPE, np.float32)
        self._c = np.zeros(_STATE_SHAPE, np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), np.float32)

    def prob(self, frame: np.ndarray) -> float:
        """Speech probability for a float32 512-sample 16kHz frame."""
        x = np.asarray(frame, np.float32).reshape(1, FRAME_SAMPLES)
        inp = np.concatenate([self._context, x], axis=1)
        out, self._h, self._c = self._session.run(
            None, {"input": inp, "h": self._h, "c": self._c}
        )
        self._context = x[:, -CONTEXT_SAMPLES:].copy()
        return float(out.reshape(-1)[0])
