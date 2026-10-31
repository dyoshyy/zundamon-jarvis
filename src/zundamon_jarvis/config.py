"""User config loaded from ~/.config/zundamon-jarvis/config.toml.

``update_config`` writes keys back (the settings window, ``zundamon-jarvisctl set``) without
rewriting the file: a top-level ``key = value`` line is replaced in place, a missing key is
added after the last top-level line (before the first ``[table]``), ``None`` removes the
line. Comments, other keys, unknown keys and tables are kept as they are. The result is
checked with ``tomllib`` before it atomically replaces the file.
"""

import json
import logging
import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from zundamon_jarvis import platform

log = logging.getLogger(__name__)

CONFIG_PATH = platform.config_dir() / "config.toml"
LEGACY_CONFIG_PATH = Path.home() / ".config" / "jarvis" / "config.toml"  # before the rename
DEFAULT_MODEL = "sonnet"
MODELS = ("sonnet", "opus", "haiku")  # what the settings window / control socket accept
DEFAULT_SPEED = 1.2  # VOICEVOX speedScale; a little faster than the engine's 1.0
SPEED_MIN = 0.8
SPEED_MAX = 2.0
Duplex = Literal["auto", "full", "half"]
DUPLEXES: tuple[str, ...] = get_args(Duplex)
LANGUAGES = ("ja", "en")  # the display language (zundamon_jarvis.i18n); speech follows the speaker


@dataclass
class Config:
    # None -> resolve 青山龍星 (ノーマル) at startup
    speaker_id: int | None = None
    # None -> follow the OS default input; else a PipeWire source name on Linux
    # ("zundamon_jarvis_aec_source" when echo cancellation is installed), a CoreAudio input
    # device name on a Mac, or a PortAudio device index
    mic_device: str | int | None = None
    # Claude model for the voice session; None (or "" in the toml) inherits the CLI default
    model: str | None = DEFAULT_MODEL
    # VOICEVOX speedScale, SPEED_MIN..SPEED_MAX
    speed: float = DEFAULT_SPEED
    # cwd of the Claude session (its CLAUDE.md / .claude/ settings load from here)
    workdir: Path = field(default_factory=Path.home)
    # optional knowledge base given to the session as an extra directory (its CLAUDE.md loads too)
    knowledge_dir: Path | None = None
    # "half": the mic is ignored while ZundamonJarvis speaks (no barge-in), for a mic that hears
    # the speaker; "full": always listening; "auto": half only on a Mac playing through its
    # built-in speakers, full otherwise (always on Linux). Auto goes by the output PortAudio
    # saw when the daemon started: after switching outputs restart it, or set "half"
    duplex: Duplex = "auto"
    # display language of the windows, menus and setup: "ja" or "en"; None follows the OS
    # (what ZundamonJarvis *says* follows the language you speak, not this)
    language: str | None = None


def valid_speed(value: Any) -> float | None:
    """The speed as a float if it is a number in range, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not SPEED_MIN - 1e-9 <= value <= SPEED_MAX + 1e-9:
        return None
    return round(value, 2)


def valid_mic_device(value: Any) -> str | int | None:
    """A mic name (a PipeWire source on Linux, a CoreAudio input device on a Mac) or a
    PortAudio device index, else None (blank, wrong type)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _dir_setting(data: dict[str, Any], key: str) -> Path | None:
    """An existing directory from ``key`` (``~`` expanded), else None (warned when set but bad)."""
    raw = data.get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        path = Path.home() / raw[2:] if raw.startswith("~/") else Path(raw)
        if raw == "~":
            path = Path.home()
        if path.is_dir():
            return path
    log.warning("config: %s %r is not an existing directory; ignoring it", key, raw)
    return None


def migrate_legacy_config(path: Path, legacy: Path | None = None) -> bool:
    """Copy the pre-rename config to ``path`` when only the old one exists. True if copied."""
    legacy = LEGACY_CONFIG_PATH if legacy is None else legacy
    if path.exists() or not legacy.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, path)
    log.info("config: copied %s to %s", legacy, path)
    return True


def load_config(path: Path = CONFIG_PATH) -> Config:
    if path == CONFIG_PATH:
        migrate_legacy_config(path)
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    speed = valid_speed(data.get("speed", DEFAULT_SPEED))
    if speed is None:
        log.warning("config: speed %r is not a number in %g..%g; using %g",
                    data.get("speed"), SPEED_MIN, SPEED_MAX, DEFAULT_SPEED)
        speed = DEFAULT_SPEED
    mic = valid_mic_device(data.get("mic_device"))
    if mic is None and "mic_device" in data:
        log.warning("config: mic_device %r is not a source name or device index; "
                    "following the OS default input", data["mic_device"])
    duplex = data.get("duplex", "auto")
    if duplex not in DUPLEXES:
        log.warning("config: duplex %r is not one of %s; using auto", duplex, "/".join(DUPLEXES))
        duplex = "auto"
    language = data.get("language") or None
    if language is not None and language not in LANGUAGES:
        log.warning("config: language %r is not one of %s; following the OS", language, "/".join(LANGUAGES))
        language = None
    return Config(
        speaker_id=data.get("speaker_id"),
        mic_device=mic,
        model=data.get("model", DEFAULT_MODEL) or None,
        speed=speed,
        workdir=_dir_setting(data, "workdir") or Path.home(),
        knowledge_dir=_dir_setting(data, "knowledge_dir"),
        duplex=duplex,
        language=language,
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"not a finite number: {value!r}")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a valid TOML basic string
    raise TypeError(f"unsupported config value: {value!r}")


_TABLE = re.compile(r"^\s*\[")


def update_config(updates: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """Set (or, with None, remove) top-level keys, keeping the rest of the file as it is."""
    if path == CONFIG_PATH:
        migrate_legacy_config(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    tomllib.loads(text)  # a broken file is left alone (TOMLDecodeError is a ValueError)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    top = next((i for i, line in enumerate(lines) if _TABLE.match(line)), len(lines))
    for key, value in updates.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        at = next((i for i in range(top) if pattern.match(lines[i])), None)
        if value is None:
            if at is not None:
                del lines[at]
                top -= 1
            continue
        new = f"{key} = {_toml_value(value)}\n"
        if at is not None:
            lines[at] = new
            continue
        last = max((i for i in range(top) if lines[i].strip()), default=-1)
        lines.insert(last + 1, new)
        top += 1
    out = "".join(lines)
    data = tomllib.loads(out)
    for key, value in updates.items():
        if data.get(key) != value:
            raise ValueError(f"could not write {key} to {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)
