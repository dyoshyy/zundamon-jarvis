import math

import pytest

from zundamon_jarvis.character import STATES, normalize, pose_for


def test_every_state_has_a_pose_at_any_time():
    for state in STATES:
        for i in range(200):
            p = pose_for(state, i * 0.037)
            assert 0.0 <= p.eye_open <= 1.0
            assert 0.0 <= p.mouth_open <= 1.0
            assert 0.0 < p.alpha <= 1.0
            assert math.isfinite(p.body_dy) and math.isfinite(p.sway)


def test_unknown_or_missing_state_is_offline():
    assert normalize(None) == "offline"
    assert normalize("off") == "offline"  # what zundamon-jarvisctl prints when no daemon runs
    assert pose_for("bogus", 1.0) == pose_for("offline", 1.0)


def test_listening_blinks_then_reopens():
    assert pose_for("listening", 0.08).eye_open < 0.1
    assert pose_for("listening", 2.0).eye_open == 1.0


def test_speaking_moves_mouth_and_bounces():
    poses = [pose_for("speaking", i * 0.02) for i in range(100)]
    assert max(p.mouth_open for p in poses) - min(p.mouth_open for p in poses) > 0.5
    assert min(p.body_dy for p in poses) < -0.05
    assert all(p.dots == 0 for p in poses)


def test_thinking_sways_and_counts_dots():
    poses = [pose_for("thinking", i * 0.1) for i in range(40)]
    assert {p.dots for p in poses} == {0, 1, 2, 3}
    assert min(p.sway for p in poses) < 0 < max(p.sway for p in poses)


def test_paused_is_dim_asleep_with_z():
    p = pose_for("paused", 1.0)
    assert p.eye_open == 0.0 and p.tint == "dim" and p.alpha < 1.0 and p.zzz >= 0.0


@pytest.mark.parametrize("state,attr", [("limit", "sweat"), ("offline", "x_eyes")])
def test_limit_and_offline_markers(state, attr):
    assert getattr(pose_for(state, 0.5), attr) is True
