"""The display language ("ja" | "en") of everything shown on screen: setup, the menu bar / tray,
the settings and conversation windows, the character's notes.

What ZundamonJarvis *says* is not covered: it answers in the language it is spoken to.

Each module keeps its own strings next to the code that shows them, as a table of
``key -> {"ja": ..., "en": ...}``, and looks them up with ``text(TABLE, key, lang, **fields)``
(``str.format`` fields). A key missing in a language falls back to Japanese, the product's
first language. ``language()`` is the choice in ``config.toml`` (``language = "ja" | "en"``) or,
unset, the OS's: Japanese when the OS is, English otherwise.
"""

import functools
import locale
import os
import string
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from zundamon_jarvis.config import LANGUAGES

DEFAULT = "ja"
Table = Mapping[str, Mapping[str, str]]


def _windows_ui_language() -> str | None:
    try:
        import ctypes

        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
    except Exception:
        return None
    return "ja" if lang_id & 0x3FF == 0x11 else "en"  # LANG_JAPANESE


def _mac_ui_language(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> str | None:
    try:
        r = run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    first = next((line.strip().strip('",') for line in (r.stdout or "").splitlines()
                  if line.strip() not in ("(", ")", "")), "")
    return ("ja" if first.startswith("ja") else "en") if first else None


def system_language(environ: Mapping[str, str] | None = None, platform_: str | None = None) -> str:
    """The OS's display language as "ja" or "en" (anything not Japanese is English)."""
    env = os.environ if environ is None else environ
    plat = platform_ or sys.platform
    if plat == "win32":
        found = _windows_ui_language()
        if found:
            return found
    elif plat == "darwin":
        found = _mac_ui_language()
        if found:
            return found
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = env.get(name)
        if value:
            return "ja" if value.lower().startswith("ja") else "en"
    try:
        guess = locale.getlocale()[0] or ""
    except ValueError:
        guess = ""
    return "ja" if guess.lower().startswith(("ja", "japanese")) else "en"


@functools.cache
def _os_language() -> str:  # asked once per process: on macOS it runs `defaults`
    return system_language()


def resolve(configured: str | None) -> str:
    return configured if configured in LANGUAGES else _os_language()


def language(config_path: Path | None = None) -> str:
    """The display language now: config.toml's ``language``, else the OS's. Read on each call
    (cheap) so a change in the settings window shows the next time a label is drawn."""
    from zundamon_jarvis.config import CONFIG_PATH, load_config

    try:
        configured = load_config(config_path or CONFIG_PATH).language
    except Exception:  # a broken config.toml must not take the labels down with it
        configured = None
    return resolve(configured)


def text(table: Table, key: str, lang: str | None = None, **fields: object) -> str:
    """``table[key]`` in ``lang`` (default: ``language()``), falling back to Japanese."""
    entry = table[key]
    s = entry.get(lang or language()) or entry[DEFAULT]
    return s.format(**fields) if fields else s


def _fields(s: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(s) if name}


def problems(table: Table) -> list[str]:
    """What is wrong with a table (for the tests): a key without both languages, or whose
    languages do not take the same ``{fields}``."""
    found = []
    for key, entry in table.items():
        missing = [lang for lang in LANGUAGES if not entry.get(lang)]
        if missing:
            found.append(f"{key}: no {'/'.join(missing)}")
        elif len({frozenset(_fields(entry[lang])) for lang in LANGUAGES}) > 1:
            found.append(f"{key}: fields differ: " + ", ".join(f"{lang}={sorted(_fields(entry[lang]))}" for lang in LANGUAGES))
    return found
