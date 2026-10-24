"""OS-specific directories. Linux paths are the long-standing XDG-ish ones; macOS uses ~/Library.

Import as ``zundamon_jarvis.platform`` (absolute imports keep the stdlib ``platform`` intact).
Every function takes an optional ``sys_`` ("linux" | "mac") so tests can pick the OS.
"""

import os
import stat
import sys
from pathlib import Path
from typing import Literal

APP = "zundamon-jarvis"


def system() -> Literal["linux", "mac"]:
    return "mac" if sys.platform == "darwin" else "linux"


def _sys(sys_: str | None) -> str:
    return sys_ or system()


def _mac_support() -> Path:
    return Path.home() / "Library" / "Application Support" / APP


def config_dir(sys_: str | None = None) -> Path:
    if _sys(sys_) == "mac":
        return _mac_support()
    return Path.home() / ".config" / APP


def data_dir(sys_: str | None = None) -> Path:
    if _sys(sys_) == "mac":
        return _mac_support()
    return Path.home() / ".local" / "share" / APP


def state_home(sys_: str | None = None) -> Path:
    """Parent of the log directory (and of the pre-rename ``jarvis`` one) on Linux."""
    if _sys(sys_) == "mac":
        return Path.home() / "Library" / "Logs"
    return Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state"))


def log_dir(sys_: str | None = None) -> Path:
    return state_home(sys_) / APP


def _private_dir(d: Path) -> Path:
    """Create ``d`` as a 0700 directory owned by us; refuse a symlink or someone else's directory."""
    try:
        d.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode):  # includes symlinks (lstat does not follow them)
        raise RuntimeError(f"{d} is not a real directory (symlink or file); refusing to use it")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"{d} is owned by uid {st.st_uid}, not {os.getuid()}; refusing to use it")
    if stat.S_IMODE(st.st_mode) != 0o700:
        d.chmod(0o700)
    return d


def runtime_dir(sys_: str | None = None) -> Path:
    if _sys(sys_) == "mac":
        return _private_dir(Path(os.environ.get("TMPDIR") or "/tmp") / f"{APP}-{os.getuid()}")
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def socket_path(sys_: str | None = None) -> Path:
    return runtime_dir(sys_) / f"{APP}.sock"
