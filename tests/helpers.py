import numpy as np


def resample_24k_to_16k(x: np.ndarray) -> np.ndarray:
    # 24k -> 16k is a 2/3 ratio: lowpass-free linear interp is enough for tests
    n_out = int(len(x) * 2 / 3)
    return np.interp(np.arange(n_out) * 1.5, np.arange(len(x)), x).astype(np.float32)


# ---- the control endpoint: a Unix socket on Linux / macOS, localhost TCP + token on Windows ----

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

WINDOWS = sys.platform == "win32"


def endpoint(directory: Path, name: str = "j") -> Path:
    """A control endpoint path in ``directory`` of this platform's form (``platform.socket_path``):
    ``<name>.json`` (TCP) on Windows, ``<name>.sock`` elsewhere (kept short: AF_UNIX paths are
    limited to ~107 bytes)."""
    return directory / f"{name}.json" if WINDOWS else directory / f"{name}.sock"


async def open_endpoint(path: Path, **kw) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """An asyncio stream to the control server at ``path``, past the TCP token handshake."""
    if path.suffix != ".json":
        return await asyncio.open_unix_connection(str(path), **kw)
    data = json.loads(path.read_text(encoding="utf-8"))
    reader, writer = await asyncio.open_connection("127.0.0.1", data["port"], **kw)
    writer.write(data["token"].encode() + b"\n")
    return reader, writer
