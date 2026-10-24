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
"""

import math
from dataclasses import dataclass

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


def normalize(state: object) -> str:
    return state if isinstance(state, str) and state in STATES else "offline"


def _blink(t: float) -> float:
    phase = t % BLINK_PERIOD_S
    if phase > BLINK_LENGTH_S:
        return 1.0
    return abs(phase / BLINK_LENGTH_S * 2 - 1)  # 1 -> 0 -> 1


def pose_for(state: object, t: float) -> Pose:
    s = normalize(state)
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
