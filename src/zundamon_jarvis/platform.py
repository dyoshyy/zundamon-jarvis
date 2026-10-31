"""OS-specific directories. Linux paths are the long-standing XDG-ish ones; macOS uses ~/Library;
Windows uses %APPDATA% (config) and %LOCALAPPDATA% (everything else).

Import as ``zundamon_jarvis.platform`` (absolute imports keep the stdlib ``platform`` intact).
Every function takes an optional ``sys_`` ("linux" | "mac" | "windows") so tests can pick the OS.

The control endpoint (``socket_path``) is a Unix socket on Linux and macOS. Windows has no
``asyncio`` Unix sockets, so there it is a small JSON file (``control.json``: the localhost TCP
port and a secret token, see ``zundamon_jarvis.transport``) in a per-user directory.
"""

import os
import stat
import sys
from pathlib import Path
from typing import Literal

APP = "zundamon-jarvis"


System = Literal["linux", "mac", "windows"]


def system() -> System:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def _sys(sys_: str | None) -> str:
    return sys_ or system()


def _mac_support() -> Path:
    return Path.home() / "Library" / "Application Support" / APP


def _win_env(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name) or str(Path.home() / fallback))


def config_dir(sys_: str | None = None) -> Path:
    s = _sys(sys_)
    if s == "mac":
        return _mac_support()
    if s == "windows":
        return _win_env("APPDATA", "AppData/Roaming") / APP
    return Path.home() / ".config" / APP


def data_dir(sys_: str | None = None) -> Path:
    s = _sys(sys_)
    if s == "mac":
        return _mac_support()
    if s == "windows":
        return _win_env("LOCALAPPDATA", "AppData/Local") / APP
    return Path.home() / ".local" / "share" / APP


def state_home(sys_: str | None = None) -> Path:
    """Parent of the log directory (and of the pre-rename ``jarvis`` one) on Linux."""
    s = _sys(sys_)
    if s == "mac":
        return Path.home() / "Library" / "Logs"
    if s == "windows":
        return data_dir(s)
    return Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state"))


def log_dir(sys_: str | None = None) -> Path:
    if _sys(sys_) == "windows":
        return data_dir("windows") / "logs"
    return state_home(sys_) / APP


def _private_dir(d: Path) -> Path:
    """Create ``d`` as a 0700 directory owned by us; refuse a symlink or someone else's directory.
    (Windows has no uid or mode bits: there the directory is under the user's own profile.)"""
    if not hasattr(os, "getuid"):
        d.mkdir(parents=True, exist_ok=True)
        return d
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
    s = _sys(sys_)
    if s == "windows":
        return _private_dir(data_dir(s) / "run")
    if s == "mac":
        return _private_dir(Path(os.environ.get("TMPDIR") or "/tmp") / f"{APP}-{os.getuid()}")
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def socket_path(sys_: str | None = None) -> Path:
    if _sys(sys_) == "windows":
        return runtime_dir(sys_) / "control.json"
    return runtime_dir(sys_) / f"{APP}.sock"
