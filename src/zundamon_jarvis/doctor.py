"""`zundamon-jarvis doctor`: one text report to paste when something does not work.

It works with the daemon down (every probe is allowed to fail) and is redacted before it is
printed:

- what was said: the ``heard`` / ``replied`` text of conversation-log lines, ``text=`` query
  values (VOICEVOX's access log has every sentence as ``/audio_query?text=%E3…``), any run of
  percent-escapes, and the rest of the line after the log messages that used to quote speech
  (``speak failed:``, ``failed to handle X(…)``, ``no answer to '…'``, ``Claude turn failed:``,
  ``utterance dropped (…):``; logs written before those were fixed) all become ``…``
- the configured ``knowledge_dir`` -> ``<knowledge_dir>``, the home path -> ``~`` (on Windows
  also with forward slashes and with JSON's doubled backslashes)
- the host name -> ``<host>``, e-mail addresses -> ``<email>``, the login and full name (and
  its parts) -> ``<user>``

Windows has no pactl, systemctl, journalctl or launchctl: the report reads the log files in
the log directory (as on a Mac), the autostart entry in the registry (HKCU ``Run`` value
``ZundamonJarvis``, made by the setup), and adds the GPUs and the CUDA device count (none:
speech recognition runs on the CPU).
"""

import getpass
import importlib.metadata
import json
import os
import platform as std_platform
import re
import shutil
import socket
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from zundamon_jarvis import platform
from zundamon_jarvis.service import RUN_KEY as WINDOWS_RUN_KEY
from zundamon_jarvis.service import RUN_VALUE as WINDOWS_RUN_VALUE

LOG_LINES = 200
VOICEVOX_LOG_LINES = 50
CONVERSATION_LINES = 10
VOICEVOX_VERSION_URL = "http://127.0.0.1:50021/version"
PACKAGES = ("zundamon-jarvis", "claude-agent-sdk", "numpy", "onnxruntime", "sounddevice",
            "httpx", "textual", "faster-whisper", "ctranslate2", "mlx-whisper", "mlx",
            "huggingface-hub", "rumps", "nvidia-cublas-cu12", "nvidia-cudnn-cu12")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# "heard": "…" in JSON and 'heard': '…' in a Python repr; escaped quotes inside are skipped.
_CONVERSATION = re.compile(r"""(["']?\b(?:heard|replied)\b["']?\s*[:=]\s*)(["'])(?:\\.|(?!\2).)*\2""")
_QUERY_TEXT = re.compile(r"""(\btext=)[^&\s"']*""")
_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2}){3,}")
_SPEECH_LOGS = [  # (pattern, replacement): the rest of the line quoted what was said
    (re.compile(r"(speak failed: ).*"), r"\1…"),
    (re.compile(r"(failed to handle \w+)\(.*"), r"\1(…)"),
    (re.compile(r"""(no answer to )['"].*"""), r"\1…"),
    (re.compile(r"(Claude turn failed: ).*"), r"\1…"),
    (re.compile(r"(utterance dropped \([^)]*\): ).*"), r"\1…"),
]
SHORT_NAME = 6  # shorter ASCII names only match between non-letters ("alice" is not in "alicia")


def _name_pattern(name: str) -> str:
    if name.isascii() and len(name) < SHORT_NAME:
        return rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])"
    return re.escape(name)


# ---- redaction ----

def redact(text: str, *, home: Path | None = None, names: Iterable[str] = (),
           hostnames: Iterable[str] = (), paths: Iterable[str] = ()) -> str:
    text = _CONVERSATION.sub(lambda m: f"{m.group(1)}{m.group(2)}…{m.group(2)}", text)
    for pattern, repl in _SPEECH_LOGS:
        text = pattern.sub(repl, text)
    text = _QUERY_TEXT.sub(r"\1…", text)
    text = _PERCENT_RUN.sub("…", text)
    text = _EMAIL.sub("<email>", text)
    for path in sorted({p.rstrip("/") for p in paths if len(p.rstrip("/")) > 1}, key=len, reverse=True):
        text = text.replace(path, "<knowledge_dir>")
    home_s = str(home if home is not None else Path.home()).rstrip("/\\")
    # Windows: C:\Users\taro, C:/Users/taro and JSON's C:\\Users\\taro (one spelling elsewhere)
    if len(home_s) > 1:
        for spelling in sorted({home_s, home_s.replace("\\", "/"), home_s.replace("\\", "\\\\")},
                               key=len, reverse=True):
            text = text.replace(spelling, "~")
    for host in sorted({h for h in hostnames if h}, key=len, reverse=True):
        text = re.sub(re.escape(host), "<host>", text, flags=re.IGNORECASE)
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(_name_pattern(name), "<user>", text, flags=re.IGNORECASE)
    return text


def _windows_full_name() -> str:
    """The account's display name ("Taro Yamada" of a Microsoft account; "" for a local one)."""
    import ctypes

    name_display = 3  # EXTENDED_NAME_FORMAT.NameDisplay
    secur32 = ctypes.windll.secur32  # type: ignore[attr-defined]
    size = ctypes.c_ulong(0)
    secur32.GetUserNameExW(name_display, None, ctypes.byref(size))
    buf = ctypes.create_unicode_buffer(size.value + 1)
    if not secur32.GetUserNameExW(name_display, buf, ctypes.byref(size)):
        return ""
    return buf.value.strip()


def _full_name(system: str) -> str:
    try:
        if system == "windows":
            return _windows_full_name()
        import pwd

        return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except Exception:
        return ""


def user_names(system: str | None = None) -> list[str]:
    """The login name, the full name and its parts (a mic may be called "Taro's AirPods")."""
    names = []
    try:
        names.append(getpass.getuser())
    except Exception:
        pass
    full = _full_name(system or platform.system())
    if full:
        names.append(full)
        # "Taro" of "Taro Yamada"; a 2-character CJK part ("太郎") is specific enough too
        names += [w for w in full.split() if len(w) >= (3 if w.isascii() else 2)]
    return [n for n in names if len(n) >= 2]


def host_names() -> list[str]:
    names = {socket.gethostname(), std_platform.node()}
    names |= {n.split(".")[0] for n in names}
    return [n for n in names if n and n != "localhost"]


# ---- probes (each one may fail; the report says so instead) ----

def _voicevox_version() -> str:
    import httpx

    r = httpx.get(VOICEVOX_VERSION_URL, timeout=2)
    r.raise_for_status()
    return str(r.json())


def _mic_names() -> list[str]:
    from zundamon_jarvis import mics

    names = [f"{s.label} ({s.name})" if s.label != s.name else s.name for s in mics.list_sources()]
    default = mics.default_source_name()
    return names + ([f"OS の既定の入力: {default}"] if default else [])


def _request(cmd: str, timeout: float = 3.0) -> dict:
    from zundamon_jarvis.control import request

    return request(cmd, timeout=timeout)


def _try(fn: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001 -- the report shows whatever went wrong
        return None, f"{type(e).__name__}: {e}"


def _tail(path: Path, n: int) -> list[str] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=n)]
    except OSError:
        return None


def _cuda_devices() -> int:
    from zundamon_jarvis import stt  # puts the cuBLAS/cuDNN DLL directories on the path first

    return stt._cuda_device_count()


def _windows_gpus(run: Callable[..., Any]) -> str:
    argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
            "(Get-CimInstance Win32_VideoController).Name"]
    out = run(argv, capture_output=True, text=True, timeout=15).stdout or ""
    return ", ".join(line.strip() for line in out.splitlines() if line.strip()) or "?"


def _os_lines(system: str, run: Callable[..., Any],
              cuda_devices: Callable[[], int] = _cuda_devices) -> list[str]:
    lines = []
    if system == "windows":
        release, version, _, _ = std_platform.win32_ver()
        edition = std_platform.win32_edition() if hasattr(std_platform, "win32_edition") else ""
        lines.append(" ".join(p for p in ("Windows", release, version, edition) if p)
                     + f" ({std_platform.machine()})")
        gpus, err = _try(lambda: _windows_gpus(run))
        lines.append(f"GPU: {gpus or err}")
        count, err = _try(cuda_devices)
        lines.append(f"CUDA デバイス: {err}" if count is None else
                     f"CUDA デバイス: {count}（音声認識は {'CUDA' if count else 'CPU（int8、遅め）'}）")
    elif system == "mac":
        ver = std_platform.mac_ver()[0] or "?"
        chip, _ = _try(lambda: run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                   text=True, timeout=5).stdout.strip())
        lines.append(f"macOS {ver} ({std_platform.machine()})")
        lines.append(f"チップ: {chip or '?'}")
    else:
        u = std_platform.uname()
        lines.append(f"{u.system} {u.release} ({u.machine})")
    lines.append(f"Python {sys.version.split()[0]} ({sys.executable})")
    return lines


def _package_lines() -> list[str]:
    out = []
    for name in PACKAGES:
        try:
            out.append(f"{name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            out.append(f"{name} なし")
    return out


def _claude_lines(status: dict | None, which: Callable[[str], str | None],
                  run: Callable[..., Any]) -> list[str]:
    path = which("claude")
    lines = [f"claude コマンド: {path or '見つかりません'}"]
    if path:
        ver, err = _try(lambda: run([path, "--version"], capture_output=True, text=True,
                                    timeout=10).stdout.strip())
        lines.append(f"claude --version: {ver or err}")
    if status is None:
        lines.append("ログイン: 不明（デーモンが動いていません。`zundamon-jarvis setup` で確かめられます）")
    elif status.get("login") is False:
        lines.append("ログイン: ログインしていません（Claude がログインを受け付けていません）")
    else:
        lines.append("ログイン: OK（拒否されていません）")
    return lines


def _log_lines(system: str, logs: Path, run: Callable[..., Any],
               which: Callable[[str], str | None]) -> list[str]:
    lines: list[str] = []
    if system in ("mac", "windows"):
        # the supervisor names the engine's log after its child ("engine"); launchd after the agent
        files = ((("supervisor.log", LOG_LINES), ("daemon.log", LOG_LINES), ("engine.log", VOICEVOX_LOG_LINES))
                 if system == "windows" else (("daemon.log", LOG_LINES), ("voicevox.log", VOICEVOX_LOG_LINES)))
        for name, n in files:
            tail = _tail(logs / name, n)
            lines.append(f"-- {logs / name}（最後の {n} 行） --")
            lines += tail if tail is not None else ["（ありません）"]
    elif which("journalctl"):
        argv = ["journalctl", "--user", "-u", "zundamon-jarvis", "-n", str(LOG_LINES), "--no-pager"]
        lines.append("-- " + " ".join(argv) + " --")
        out, err = _try(lambda: run(argv, capture_output=True, text=True, timeout=10).stdout)
        lines += (out or err or "").splitlines()
    else:
        lines.append("（journalctl がないのでログは取れません）")
    days = sorted(logs.glob("*.jsonl")) if logs.is_dir() else []
    if days:
        tail = _tail(days[-1], CONVERSATION_LINES) or []
        lines.append(f"-- {days[-1]}（会話ログの最後の {CONVERSATION_LINES} 行、中身は伏せ字） --")
        lines += tail
    return lines


MIC_SILENT_VERDICT = (
    "判定: デーモンのマイクは完全な無音です（音の届くフレームが全部 0）。"
    "macOS がデーモンの Python（上の「デーモンの Python」）にマイクを許可していない可能性が高いです")
# Windows hands an app without the microphone permission frames of zeros, like macOS.
WINDOWS_MIC_SILENT_VERDICT = (
    "判定: デーモンのマイクは完全な無音です（音の届くフレームが全部 0）。"
    "Windows の「設定 > プライバシーとセキュリティ > マイク」で「マイクへのアクセス」か"
    "「デスクトップ アプリがマイクにアクセスできるようにする」がオフになっている可能性が高いです")


def mic_verdict(debug: dict | None, system: str | None = None) -> str | None:
    """A one-line verdict when the daemon reads frames that are all exact zeros (macOS or
    Windows: no microphone permission)."""
    if not isinstance(debug, dict) or debug.get("paused") or debug.get("ears_muted"):
        return None
    if (debug.get("frames_last_5s") or 0) > 0 and debug.get("max_abs_last_5s") == 0:
        return WINDOWS_MIC_SILENT_VERDICT if system == "windows" else MIC_SILENT_VERDICT
    return None


def _windows_autostart() -> str | None:
    """The command of the HKCU Run entry that starts ZundamonJarvis at login, or None."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
        try:
            value, _ = winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
        except FileNotFoundError:
            return None
    return str(value)


def _autostart_lines(autostart: Callable[[], str | None]) -> list[str]:
    where = f"HKCU\\{WINDOWS_RUN_KEY}\\{WINDOWS_RUN_VALUE}"
    command, err = _try(autostart)
    if err:
        return [f"自動起動 ({where}): 読めません（{err}）"]
    return [f"自動起動 ({where}): {command or '登録されていません'}"]


def _agent_python(home: Path) -> str | None:
    """macOS, daemon down: the interpreter in the shebang of the LaunchAgent's executable."""
    import plistlib

    from zundamon_jarvis.service import LABELS

    try:
        plist = plistlib.loads((home / "Library" / "LaunchAgents" / f"{LABELS['daemon']}.plist").read_bytes())
        with open(plist["ProgramArguments"][0], "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not first.startswith("#!"):
        return None
    return os.path.realpath(first[2:].split()[0])


def _knowledge_paths(config_path: Path) -> list[str]:
    """Spellings of the configured knowledge_dir, to hide wherever they show up."""
    import tomllib

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8")).get("knowledge_dir")
    except (OSError, ValueError):
        return []
    if not isinstance(raw, str) or raw.strip() in ("", "~"):
        return []
    full = Path(raw).expanduser()
    return [raw, str(full), os.path.realpath(full)]


_KNOWLEDGE_LINE = re.compile(r"^(\s*knowledge_dir\s*=).*$", re.MULTILINE)


def collect(
    *,
    system: str | None = None,
    request: Callable[..., dict] = _request,
    voicevox_version: Callable[[], str] = _voicevox_version,
    mic_names: Callable[[], list[str]] = _mic_names,
    run: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    config_path: Path | None = None,
    log_dir: Path | None = None,
    cuda_devices: Callable[[], int] = _cuda_devices,
    autostart: Callable[[], str | None] = _windows_autostart,
) -> str:
    """The unredacted report."""
    sys_ = system or platform.system()
    config_path = config_path or platform.config_dir(sys_) / "config.toml"
    logs = log_dir or platform.log_dir(sys_)
    out: list[str] = ["# zundamon-jarvis doctor"]

    def section(title: str, lines: Iterable[str]) -> None:
        out.extend(["", f"== {title} ==", *lines])

    section("OS / Python", _os_lines(sys_, run, cuda_devices))
    section("パッケージ", _package_lines())

    mics, err = _try(mic_names)
    section("マイク", mics or [err or "（見つかりません）"])

    ver, err = _try(voicevox_version)
    section("VOICEVOX", [f"/version: {ver}" if ver else f"つながりません（{err}）"])

    status, s_err = _try(lambda: request("status", timeout=2))
    debug, _ = _try(lambda: request("debug", timeout=2)) if status else (None, None)
    section("Claude", _claude_lines(status, which, run))

    daemon = ([f"status: {json.dumps(status, ensure_ascii=False)}"] if status
              else [f"動いていません（{s_err}）"])
    info = debug.get("debug") if isinstance(debug, dict) else None
    python = info.get("python") if isinstance(info, dict) else None
    if python:
        daemon.append(f"デーモンの Python: {python}")
    elif sys_ == "mac" and (agent := _agent_python(Path.home())):
        daemon.append(f"デーモンの Python（起動設定から）: {agent}")
    if info is not None:
        daemon.append("debug: " + json.dumps(info, ensure_ascii=False))
        if verdict := mic_verdict(info, sys_):
            daemon.append(verdict)
    if sys_ == "windows":  # no service manager: started at login by a Run entry
        section("デーモン", daemon + ["", *_autostart_lines(autostart)])
    else:
        from zundamon_jarvis.service import service_status

        services, err = _try(lambda: service_status(sys_, run=run))
        section("デーモン", daemon + ["", "サービス:", services or err or ""])

    text = None
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        pass
    text = _KNOWLEDGE_LINE.sub(r"\1 <設定あり>", text) if text else text
    section(f"設定 ({config_path})", (text or "（まだありません）").splitlines())

    section("ログ", _log_lines(sys_, logs, run, which))
    return "\n".join(out)


def run_doctor(out: Callable[[str], None] = print, *, names: Iterable[str] | None = None,
               hostnames: Iterable[str] | None = None, home: Path | None = None,
               **collect_kw: Any) -> int:
    report = collect(**collect_kw)
    config_path = (collect_kw.get("config_path")
                   or platform.config_dir(collect_kw.get("system")) / "config.toml")
    out(redact(report, home=home or Path.home(),
               names=user_names(collect_kw.get("system")) if names is None else names,
               hostnames=host_names() if hostnames is None else hostnames,
               paths=_knowledge_paths(config_path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    """`zundamon-jarvis doctor` (dispatched by zundamon_jarvis.cli)."""
    if argv:
        print("usage: zundamon-jarvis doctor", file=sys.stderr)
        return 2
    return run_doctor()
