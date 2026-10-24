import numpy as np


def resample_24k_to_16k(x: np.ndarray) -> np.ndarray:
    # 24k -> 16k is a 2/3 ratio: lowpass-free linear interp is enough for tests
    n_out = int(len(x) * 2 / 3)
    return np.interp(np.arange(n_out) * 1.5, np.arange(len(x)), x).astype(np.float32)
