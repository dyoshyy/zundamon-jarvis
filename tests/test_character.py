import math

import pytest

from zundamon_jarvis.character import (
    CLICK_SLOP_PX,
    LINGER_S,
    MIN_SHOW_S,
    NOTE_S,
    STATES,
    TEXT_MAX,
    Bubble,
    Line,
    Prefs,
    clamp_margins,
    is_click,
    load_prefs,
    normalize,
    pose_for,
    save_prefs,
)


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


# ---- hearing ----

def test_hearing_shows_waves_and_a_lit_antenna_while_awake():
    for state in ("listening", "thinking", "speaking", "limit"):
        p = pose_for(state, 0.3, hearing=True)
        assert 0.0 <= p.waves < 1.0 and p.glow and p.eye_open == 1.0
        assert pose_for(state, 0.3).waves < 0 and not pose_for(state, 0.3).glow


def test_hearing_is_not_shown_asleep_or_offline():
    for state in ("paused", "offline"):
        assert pose_for(state, 0.3, hearing=True) == pose_for(state, 0.3)


def test_waves_move_outward_over_time():
    phases = {round(pose_for("listening", i * 0.1, hearing=True).waves, 2) for i in range(20)}
    assert len(phases) > 5


# ---- bubble ----

def live_bubble():
    b = Bubble()
    b.feed({"kind": "hello", "boot": "b1"}, 0.0)
    return b


def test_history_before_live_is_never_shown():
    b = live_bubble()
    b.feed({"kind": "said", "text": "昔の返事", "what": "reply"}, 0.0)
    assert b.show("speaking", 0.1) is None
    b.feed({"kind": "live"}, 0.2)
    b.feed({"kind": "said", "text": "今の返事", "what": "reply"}, 0.3)
    assert b.show("speaking", 0.4) == Line("me", "今の返事")


def test_said_stays_while_speaking_then_lingers_and_goes():
    b = live_bubble()
    b.feed({"kind": "live"}, 0.0)
    b.feed({"kind": "said", "text": "明日はジムだよ。", "what": "reply"}, 1.0)
    for t in (1.5, 5.0, 9.0):
        assert b.show("speaking", t) is not None
    assert b.show("listening", 9.0 + LINGER_S - 0.1) is not None
    assert b.show("listening", 9.0 + LINGER_S + 0.1) is None
    assert b.show("listening", 99.0) is None  # gone for good


def test_said_shows_at_least_a_moment_even_if_speaking_was_never_seen():
    b = live_bubble()
    b.feed({"kind": "live"}, 0.0)
    b.feed({"kind": "said", "text": "うん。", "what": "filler"}, 10.0)
    assert b.show("listening", 10.0 + MIN_SHOW_S - 0.1) == Line("me", "うん。")
    assert b.show("listening", 10.0 + MIN_SHOW_S + 0.1) is None


def test_heard_shows_what_you_said_until_the_reply():
    b = live_bubble()
    b.feed({"kind": "live"}, 0.0)
    b.feed({"kind": "heard", "text": "明日の予定は？"}, 1.0)
    assert b.show("thinking", 20.0) == Line("you", "明日の予定は？")  # thinking keeps it
    b.feed({"kind": "said", "text": "ジムだよ。", "what": "reply"}, 21.0)
    assert b.show("speaking", 21.1) == Line("me", "ジムだよ。")


def test_speaking_again_clears_your_old_words_but_not_the_reply():
    b = live_bubble()
    b.feed({"kind": "live"}, 0.0)
    b.feed({"kind": "heard", "text": "一つ目"}, 1.0)
    b.feed({"kind": "hearing", "on": True}, 1.5)
    assert b.show("thinking", 1.6) is None
    b.feed({"kind": "said", "text": "返事", "what": "reply"}, 2.0)
    b.feed({"kind": "hearing", "on": True}, 2.5)
    assert b.show("speaking", 2.6) == Line("me", "返事")


def test_a_reconnect_starts_over():
    b = live_bubble()
    b.feed({"kind": "live"}, 0.0)
    b.feed({"kind": "said", "text": "返事", "what": "reply"}, 1.0)
    b.feed({"kind": "hello", "boot": "b2"}, 1.5)
    assert b.show("speaking", 1.6) is None
    b.feed({"kind": "said", "text": "履歴", "what": "reply"}, 1.7)
    assert b.show("speaking", 1.8) is None


def test_long_text_is_cut_and_whitespace_folded():
    b = live_bubble()
    b.feed({"kind": "live"}, 0.0)
    b.feed({"kind": "said", "text": "あ\n  い" + "う" * 500, "what": "reply"}, 1.0)
    line = b.show("speaking", 1.1)
    assert line.text.startswith("あ い") and len(line.text) == TEXT_MAX and line.text.endswith("…")


def test_a_note_shows_for_a_fixed_time_whatever_the_state():
    b = live_bubble()
    b.note("Speech bubble: off", 5.0)
    assert b.show("speaking", 5.0 + NOTE_S - 0.1) == Line("note", "Speech bubble: off")
    assert b.show("speaking", 5.0 + NOTE_S + 0.1) is None


# ---- moving it around ----

def test_small_moves_are_clicks():
    assert is_click(0, 0) and is_click(3, -4)
    assert not is_click(CLICK_SLOP_PX + 1, 0) and not is_click(0, -20)


def test_margins_keep_the_window_on_screen():
    assert clamp_margins(-50, 9999, (300, 290), (1920, 1080)) == (0, 1080 - 290)
    assert clamp_margins(100, 200, (300, 290), (1920, 1080)) == (100, 200)
    assert clamp_margins(100, 200, (300, 290), (200, 200)) == (0, 0)  # screen too small


def test_prefs_round_trip_and_survive_junk(tmp_path):
    path = tmp_path / "state" / "character.json"
    assert load_prefs(path) == Prefs()
    save_prefs(path, Prefs(right=120, bottom=40, bubble=False))
    assert load_prefs(path) == Prefs(right=120, bottom=40, bubble=False)
    path.write_text("{not json")
    assert load_prefs(path) == Prefs()
    path.write_text('{"right": "x", "bottom": -5, "bubble": "yes", "extra": 1}')
    assert load_prefs(path) == Prefs(bottom=0)
