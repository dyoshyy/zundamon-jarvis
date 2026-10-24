import logging

from zundamon_jarvis.diag import RateLimitedLog, Window


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_rate_limited_log_caps_each_key_and_reports_the_suppressed(caplog):
    clock = Clock()
    log = RateLimitedLog(logging.getLogger("t"), per_window=2, window_s=10, clock=clock)
    with caplog.at_level(logging.INFO):
        for i in range(5):
            log(logging.INFO, "a", "line %d", i)
        log(logging.INFO, "b", "other")
        clock.t = 11
        log(logging.INFO, "a", "line %d", 9)
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["line 0", "line 1", "other", "a: 3 similar lines suppressed", "line 9"]


def test_window_keeps_recent_values_only():
    clock = Clock()
    w = Window(30.0, clock=clock)
    assert w.stats() == {"n": 0, "max": None, "mean": None}
    for t, v in [(0, 0.1), (10, 0.9), (29, 0.2)]:
        clock.t = t
        w.add(v)
    clock.t = 35  # the 0.1 at t=0 has left the 30 s window
    assert w.stats() == {"n": 2, "max": 0.9, "mean": 0.55}
    assert w.stats(last_s=10) == {"n": 1, "max": 0.2, "mean": 0.2}
    assert w.count(last_s=10) == 1
    assert w.last_age() == 6.0
