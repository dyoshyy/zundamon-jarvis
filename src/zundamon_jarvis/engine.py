"""Pinned, checksummed install of the VOICEVOX engine (Linux NVIDIA / macOS arm64 / Windows x64).

Linux and macOS get the ``.7z`` archives, unpacked with bsdtar (libarchive). Windows gets the
DirectML ``.vvpp``: DirectML runs on any DirectX 12 GPU (NVIDIA, AMD, Intel) and falls back to
the CPU, so one build fits every PC, and it is a single file. A ``.vvpp`` is a plain ZIP (with
``run.exe`` at its root), so the stdlib ``zipfile`` unpacks it: nothing to install first.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

from zundamon_jarvis import platform

_BASE = "https://github.com/VOICEVOX/voicevox_engine/releases/download/0.26.0"

# Pinned from https://github.com/VOICEVOX/voicevox_engine/releases/tag/0.26.0 — each (url, sha256)
# is the asset's `digest` as published by GitHub. Multi-part archives list every part in order.
ENGINE: dict = {
    "version": "0.26.0",
    "mac": (
        (
            f"{_BASE}/voicevox_engine-macos-arm64-0.26.0.7z.001",
            "76e445f13e142d05a0fdb6a9aeb8935589a0e40654d24185c338739e711f5d8f",
        ),
    ),
    "linux": (
        (
            f"{_BASE}/voicevox_engine-linux-nvidia-0.26.0.7z.001",
            "90b34eb13500679ac963766e172465f30eea12a0a7973dfd969c39595367b082",
        ),
        (
            f"{_BASE}/voicevox_engine-linux-nvidia-0.26.0.7z.002",
            "cf8e0ed723b3bffc15be5f6324e6198b27a1c5b1550601c3e7a5471a55d84dd8",
        ),
    ),
    "linux-cpu": (
        (
            f"{_BASE}/voicevox_engine-linux-cpu-x64-0.26.0.7z.001",
            "3a74cf1bdef6b415f2acf7501f50b43e8d1ebcff85b923f693a7c4a60734d943",
        ),
    ),
    "windows": (  # 1936066614 bytes
        (
            f"{_BASE}/voicevox_engine-windows-directml-0.26.0.vvpp",
            "7c2f85dfb88160c902297e5d7002ce781a48167e7f28def7a1f10f11390f9bec",
        ),
    ),
}

ZIP_SUFFIXES = (".vvpp", ".zip")

MARKER = ".zundamon-engine-version"


class ChecksumError(RuntimeError):
    pass


# progress(stage, done, total): stage is "download" (bytes of every part together; total None
# until known), "extract" or "check" (done = total = 0: no measure, just the stage).
Progress = Callable[[str, int, int | None], None]


def default_dest(system: str) -> Path:
    return platform.data_dir(system) / "voicevox_engine"


def run_name(system: str) -> str:
    """The engine's executable inside its directory."""
    return "run.exe" if system == "windows" else "run"


def run_path(system: str, dest: Path | None = None) -> Path:
    return (Path(dest) if dest else default_dest(system)) / run_name(system)


def tar_command(system: str) -> str:
    """The archiver for the .7z builds (Windows' .vvpp never needs one: see ``extract_archive``)."""
    if system == "mac":
        return "/usr/bin/tar"
    return "tar" if system == "windows" else "bsdtar"


def _is_zip(parts: Sequence[Path]) -> bool:
    return len(parts) == 1 and Path(parts[0]).suffix.lower() in ZIP_SUFFIXES


def _no_window() -> dict:
    """Windows: no console window flashes up when a GUI process (pythonw) runs the engine."""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def fetch_url(url: str, dest: Path, report: Callable[[int, int | None], None] | None = None) -> None:
    """Download ``url`` to ``dest``; ``report(bytes_so_far, content_length)`` after each chunk."""
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:  # noqa: S310 - pinned https URLs
        if report is None:
            shutil.copyfileobj(r, f, 1 << 20)
            return
        length = r.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            report(done, total)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_archive(parts: Sequence[Path], into: Path, tar: str) -> None:
    """A single .vvpp/.zip is unpacked with ``zipfile`` (which strips drive letters, leading
    slashes and ``..`` from member paths) and deleted. Otherwise concatenate the verified parts
    into one .7z (libarchive's 7z reader needs random access, so it cannot be streamed), extract
    it with ``tar``, then delete it."""
    if _is_zip(parts):
        try:
            with zipfile.ZipFile(parts[0]) as z:
                z.extractall(into)
        finally:
            Path(parts[0]).unlink(missing_ok=True)
        return
    joined = Path(into).parent / "engine.7z"
    try:
        with open(joined, "wb") as out:
            for part in parts:
                with open(part, "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)
                Path(part).unlink()  # keep peak disk use near one copy
        subprocess.run([tar, "-xf", str(joined), "-C", str(into)], check=True)
    finally:
        joined.unlink(missing_ok=True)


def check_runs(run: Path) -> None:
    # The engine's argparse has no --version; --help exits 0 once the binary and its libs load.
    subprocess.run([str(run), "--help"], check=True, capture_output=True, timeout=120,
                   **_no_window())


def _installed(dest: Path, system: str) -> bool:
    run = dest / run_name(system)
    # Windows has no execute bit (os.access X_OK is true for any file there): run.exe existing is it.
    if not (run.is_file() and (system == "windows" or os.access(run, os.X_OK))):
        return False
    marker = dest / MARKER
    # An install made before pinning has no marker: keep it rather than re-download ~3 GB.
    return not marker.exists() or marker.read_text().strip() == ENGINE["version"]


def archive_root(out: Path, system: str) -> Path:
    """The engine directory in an unpacked archive: ``out`` itself (the .vvpp has ``run.exe`` at
    its root) or its only top-level folder (the .7z builds)."""
    name = run_name(system)
    tops = list(out.iterdir())
    if not (out / name).is_file() and len(tops) == 1 and tops[0].is_dir():
        out = tops[0]
    if not (out / name).is_file():
        raise RuntimeError(f"extracted archive has no `{name}` executable")
    return out


def _swap_in(new: Path, dest: Path, tmp: Path) -> None:
    """Replace dest with new; the old install is kept aside and restored if the move fails."""
    old = tmp / "old"
    had = dest.exists() or dest.is_symlink()
    if had:
        os.replace(dest, old)  # renames the link itself when dest is a symlink
    try:
        os.replace(new, dest)
    except BaseException:
        if had:
            os.replace(old, dest)
        raise
    if had:
        if old.is_symlink():
            old.unlink()
        else:
            shutil.rmtree(old, ignore_errors=True)


def install_engine(
    system: str,
    dest: Path | None = None,
    flavor: str | None = None,
    fetch: Callable[[str, Path], None] = fetch_url,
    extract: Callable[[Sequence[Path], Path, str], None] = extract_archive,
    check: Callable[[Path], None] = check_runs,
    progress: Progress | None = None,
) -> Path:
    """Download, verify and unpack the pinned engine into ``dest`` (nothing to do when it is
    there already). With ``progress`` the download goes ``fetch(url, path, report)``."""
    dest = Path(dest) if dest else default_dest(system)
    if _installed(dest, system):
        print(f"VOICEVOX engine {ENGINE['version']} already installed: {dest}")
        return dest
    if flavor == "linux-cpu" and system != "linux":
        raise ValueError("--cpu is only available on Linux")
    parts_spec = ENGINE[flavor or system]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".engine-dl.", dir=dest.parent))
    try:
        files: list[Path] = []
        before = 0  # bytes of the parts already fetched
        for url, want in parts_spec:
            f = tmp / url.rsplit("/", 1)[1]
            if progress is None:
                fetch(url, f)
            else:
                def report(done: int, total: int | None, base: int = before) -> None:
                    progress("download", base + done, None if total is None or len(parts_spec) > 1
                             else total)
                fetch(url, f, report)  # type: ignore[call-arg]
                before += f.stat().st_size
            got = sha256_file(f)
            if got != want:
                f.unlink(missing_ok=True)
                raise ChecksumError(f"SHA-256 mismatch for {url}: expected {want}, got {got}")
            files.append(f)
        out = tmp / "out"
        out.mkdir()
        if progress is not None:
            progress("extract", 0, 0)
        extract(files, out, tar_command(system))
        # extract_archive owns part cleanup (parts may already be gone)
        for f in files:
            f.unlink(missing_ok=True)
        root = archive_root(out, system)
        if progress is not None:
            progress("check", 0, 0)
        check(root / run_name(system))
        (root / MARKER).write_text(ENGINE["version"])
        _swap_in(root, dest, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"VOICEVOX engine {ENGINE['version']} installed: {dest}")
    return dest
