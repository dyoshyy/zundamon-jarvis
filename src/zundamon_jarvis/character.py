"""Pure state -> animation mapping for the on-screen character (``zundamon-jarvisctl character``).

No GUI imports here: ``pose_for(state, t)`` turns the daemon's state (the ``state`` field of the
control socket's ``status`` reply) and a time in seconds into a ``Pose``, which the GTK drawing
code (``zundamon_jarvis.character_app``) paints. Everything is deterministic in ``t`` so it is testable.

- listening: slow breathing, a blink every few seconds.
- thinking: sways side to side, eyes look up, dots count up above the head.
- speaking: the body bounces and the mouth opens and closes.
- paused: dim and sleepy, eyes shut, floating "z".
- limit: worried face, warm tint, a sweat drop.
- offline (or an unknown state / no daemon): grey, X eyes, almost transparent.
- hearing (the user's speech is being taken in; the ``hearing`` watch event): eyes wide open,
  sound waves beside the head and a lit antenna, on top of any awake state.

Also the rest of the overlay that needs no GTK: ``Bubble`` decides what the speech bubble shows
(from the control socket's ``watch`` stream), ``Prefs`` is where the character was left and
whether the bubble is on, ``clamp_margins`` / ``is_click`` are the dragging rules.
"""

import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zundamon_jarvis import platform

STATES = ("listening", "thinking", "speaking", "paused", "limit", "offline")
BLINK_PERIOD_S = 4.0
BLINK_LENGTH_S = 0.16


@dataclass(frozen=True)
class Pose:
    body_dy: float = 0.0  # vertical offset, in units of the body height (+ is down)
    squash: float = 0.0  # +: taller and thinner, -: shorter and wider
    sway: float = 0.0  # rotation in radians around the feet
    eye_open: float = 1.0  # 0 shut .. 1 open
    eye_look_y: float = 0.0  # -1 up .. 1 down
    mouth_open: float = 0.0  # 0 smile .. 1 wide open
    mouth_flat: bool = False  # worried / flat mouth instead of a smile
    dots: int = 0  # thinking dots above the head
    zzz: float = -1.0  # sleep "z" phase 0..1, or -1 for none
    sweat: bool = False
    x_eyes: bool = False
    alpha: float = 1.0
    tint: str = "normal"  # normal | dim | warm | grey
    waves: float = -1.0  # hearing: sound waves phase 0..1 (moving outward), or -1 for none
    glow: bool = False  # hearing: the antenna bulb is lit


def normalize(state: object) -> str:
    return state if isinstance(state, str) and state in STATES else "offline"


def _blink(t: float) -> float:
    phase = t % BLINK_PERIOD_S
    if phase > BLINK_LENGTH_S:
        return 1.0
    return abs(phase / BLINK_LENGTH_S * 2 - 1)  # 1 -> 0 -> 1


HEARING_WAVE_S = 0.6  # one wave travels out in this long


def pose_for(state: object, t: float, hearing: bool = False) -> Pose:
    pose = _pose(normalize(state), t)
    if not hearing or normalize(state) in ("paused", "offline"):  # the ears are closed then
        return pose
    return replace(pose, waves=(t / HEARING_WAVE_S) % 1.0, glow=True, eye_open=1.0,
                   eye_look_y=0.0)


def _pose(s: str, t: float) -> Pose:
    if s == "listening":
        breath = math.sin(t * 2 * math.pi / 3.2)
        return Pose(squash=0.025 * breath, body_dy=-0.01 * breath, eye_open=_blink(t),
                    mouth_open=0.0)
    if s == "thinking":
        return Pose(sway=0.09 * math.sin(t * 2 * math.pi / 2.2), eye_look_y=-1.0,
                    eye_open=_blink(t + 1.3), dots=int(t * 2.5) % 4, mouth_flat=True)
    if s == "speaking":
        bounce = abs(math.sin(t * 2 * math.pi * 2.4))
        talk = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 5.0 + 0.8 * math.sin(t * 2.3))
        return Pose(body_dy=-0.07 * bounce, squash=0.04 * (1 - bounce) - 0.02,
                    mouth_open=0.15 + 0.85 * talk, eye_open=_blink(t))
    if s == "paused":
        breath = math.sin(t * 2 * math.pi / 5.0)
        return Pose(squash=0.015 * breath, body_dy=0.04, sway=0.03 * breath, eye_open=0.0,
                    zzz=(t / 3.0) % 1.0, alpha=0.6, tint="dim")
    if s == "limit":
        shake = math.sin(t * 2 * math.pi * 1.2)
        return Pose(sway=0.02 * shake, eye_open=_blink(t), mouth_flat=True, sweat=True,
                    tint="warm")
    return Pose(body_dy=0.05, eye_open=0.0, x_eyes=True, mouth_flat=True, alpha=0.35,
                tint="grey")


# ---- speech bubble ----

MIN_SHOW_S = 2.5  # a line stays at least this long
LINGER_S = 2.5  # and this long after ZundamonJarvis was last thinking / speaking
NOTE_S = 1.5  # a note (the bubble was switched on / off) shows this long
TEXT_MAX = 140
_BUSY = ("thinking", "speaking")


@dataclass(frozen=True)
class Line:
    who: str  # "you" (what was heard) | "me" (what ZundamonJarvis says) | "note"
    text: str


def _fold(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


class Bubble:
    """What the speech bubble shows. ``feed`` takes ``watch`` events, ``show`` is asked every
    frame. Only live events count (the replayed history is old news). The last thing heard
    or said is shown while ZundamonJarvis works on it and fades after; the user starting
    to talk again clears what they said before (not the reply)."""

    def __init__(self) -> None:
        self._live = False
        self._line: Line | None = None
        self._since = 0.0
        self._until: float | None = None  # a note's fixed end
        self._busy_at = -math.inf

    def _set(self, line: Line | None, now: float, until: float | None = None) -> None:
        self._line, self._since, self._until = line, now, until

    def feed(self, ev: dict, now: float) -> None:
        kind = ev.get("kind")
        if kind == "hello":
            self._live = False
            self._set(None, now)
            return
        if kind == "live":
            self._live = True
            return
        if not self._live:
            return
        if kind == "heard":
            self._set(Line("you", _fold(ev.get("text"))), now)
        elif kind == "said":
            self._set(Line("me", _fold(ev.get("text"))), now)
        elif kind == "hearing" and ev.get("on") and self._line and self._line.who == "you":
            self._set(None, now)

    def note(self, text: str, now: float) -> None:
        self._set(Line("note", text), now, until=now + NOTE_S)

    def show(self, state: str, now: float) -> Line | None:
        if state in _BUSY:
            self._busy_at = now
        if self._line is None:
            return None
        end = self._until if self._until is not None else max(
            self._since + MIN_SHOW_S, self._busy_at + LINGER_S)
        if now > end:
            self._set(None, now)
        return self._line


# ---- moving it around, and remembering where ----

MARGIN = 16  # default distance from the bottom-right corner
CLICK_SLOP_PX = 6  # a press that moves less than this is a click, not a drag


def is_click(dx: float, dy: float) -> bool:
    return abs(dx) <= CLICK_SLOP_PX and abs(dy) <= CLICK_SLOP_PX


def clamp_margins(right: float, bottom: float, size: tuple[int, int],
                  screen: tuple[int, int]) -> tuple[int, int]:
    """Margins from the bottom-right corner that keep a ``size`` window on a ``screen``."""
    (w, h), (sw, sh) = size, screen
    return (int(min(max(right, 0), max(sw - w, 0))), int(min(max(bottom, 0), max(sh - h, 0))))


@dataclass(frozen=True)
class Prefs:
    right: int = MARGIN
    bottom: int = MARGIN
    bubble: bool = True


def prefs_path() -> Path:
    return platform.log_dir() / "character.json"


def load_prefs(path: Path) -> Prefs:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return Prefs()
    if not isinstance(raw, dict):
        return Prefs()
    d = Prefs()

    def margin(key: str, default: int) -> int:
        v = raw.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        return max(int(v), 0) if ok else default

    bubble = raw.get("bubble")
    return Prefs(right=margin("right", d.right), bottom=margin("bottom", d.bottom),
                 bubble=bubble if isinstance(bubble, bool) else d.bubble)


def save_prefs(path: Path, prefs: Prefs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"right": prefs.right, "bottom": prefs.bottom,
                               "bubble": prefs.bubble}))
    os.replace(tmp, path)
