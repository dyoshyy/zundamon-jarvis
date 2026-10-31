import logging
import os
import sys
import time

import pytest

from zundamon_jarvis import supervisor
from zundamon_jarvis.supervisor import ChildSpec, InstanceLock, Policy, Supervisor


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 1


class Spawner:
    def __init__(self, fail=()):
        self.started: list[tuple[str, FakeProc]] = []
        self.fail = set(fail)

    def __call__(self, spec):
        if spec.name in self.fail:
            raise FileNotFoundError(spec.argv[0])
        p = FakeProc(100 + len(self.started))
        self.started.append((spec.name, p))
        return p

    def names(self):
        return [n for n, _ in self.started]

    def last(self, name):
        return [p for n, p in self.started if n == name][-1]


def make(fail=()):
    clock, spawn = Clock(), Spawner(fail)
    specs = [ChildSpec(s.name, s.argv, s.policy)  # no port probe: nothing real listens in tests
             for s in supervisor.child_specs("windows", r"C:\zj\zundamon-jarvis.exe")]
    return Supervisor(specs, spawn, clock), clock, spawn


def test_child_specs_mirror_the_units():
    engine, daemon, tray = supervisor.child_specs("windows", "zj.exe")
    assert engine.argv[0].endswith("run.exe")
    assert engine.argv[1:] == ["--host", "127.0.0.1", "--port", "50021", "--use_gpu"]  # like voicevox.service
    assert daemon.argv == ["zj.exe", "daemon"] and tray.argv == ["zj.exe", "tray"]
    assert daemon.policy.prevent == {3}  # RestartPreventExitStatus=3
    assert (daemon.policy.delay_s, daemon.policy.burst, daemon.policy.interval_s) == (5, 5, 300)
    assert engine.policy.restart == "always" and tray.policy.restart == "on-failure"


def test_first_tick_starts_everything():
    sup, _, spawn = make()
    sup.tick()
    assert spawn.names() == ["engine", "daemon", "tray"]
    assert all(sup.state(n) == "running" for n in ("engine", "daemon", "tray"))


@pytest.mark.parametrize("code", [1, 4, -9])
def test_daemon_failure_restarts_after_the_delay(code):
    sup, clock, spawn = make()
    sup.tick()
    spawn.last("daemon").code = code
    sup.tick()
    assert sup.state("daemon") == "waiting"
    clock.t += 4.9
    sup.tick()
    assert spawn.names().count("daemon") == 1
    clock.t += 0.2
    sup.tick()
    assert spawn.names().count("daemon") == 2 and sup.state("daemon") == "running"


@pytest.mark.parametrize("code", [0, 3])
def test_daemon_quit_or_already_running_stays_down(code):
    sup, clock, spawn = make()
    sup.tick()
    spawn.last("daemon").code = code
    for _ in range(5):
        sup.tick()
        clock.t += 10
    assert spawn.names().count("daemon") == 1
    assert sup.state("daemon") == "stopped"
    assert sup.state("engine") == "running"  # the others are not affected


def test_quitting_the_tray_keeps_it_quit_but_a_crash_restarts_it():
    sup, clock, spawn = make()
    sup.tick()
    spawn.last("tray").code = 1
    sup.tick()
    clock.t += 5
    sup.tick()
    assert spawn.names().count("tray") == 2
    spawn.last("tray").code = 0
    sup.tick()
    clock.t += 60
    sup.tick()
    assert spawn.names().count("tray") == 2 and sup.state("tray") == "stopped"


def test_the_engine_is_kept_running_even_after_a_clean_exit():
    sup, clock, spawn = make()
    sup.tick()
    spawn.last("engine").code = 0
    sup.tick()
    clock.t += 5
    sup.tick()
    assert spawn.names().count("engine") == 2


def test_a_crash_loop_gives_up_after_five_starts_in_five_minutes():
    sup, clock, spawn = make()
    for _ in range(20):
        sup.tick()
        if sup.state("daemon") == "running":
            spawn.last("daemon").code = 1  # dies right away
        clock.t += 5
    assert spawn.names().count("daemon") == 5
    assert sup.state("daemon") == "stopped"
    clock.t += 1000  # a start limit hit is final, as in systemd
    sup.tick()
    assert spawn.names().count("daemon") == 5


def test_crashes_spread_out_keep_being_restarted():
    sup, clock, spawn = make()
    sup.tick()
    for _ in range(10):
        clock.t += 100
        spawn.last("daemon").code = 4
        sup.tick()
        clock.t += 5
        sup.tick()
    assert spawn.names().count("daemon") == 11 and sup.state("daemon") == "running"


def test_a_missing_engine_counts_as_failed_starts():
    sup, clock, spawn = make(fail={"engine"})
    for _ in range(10):
        sup.tick()
        clock.t += 5
    assert sup.state("engine") == "stopped"
    assert spawn.names().count("daemon") == 1  # the rest still run


def test_an_engine_already_listening_is_waited_out_without_counting():
    taken = [True]
    clock, spawn = Clock(), Spawner()
    spec = ChildSpec("engine", ["run.exe"], Policy(restart="always"), served_elsewhere=lambda: taken[0])
    sup = Supervisor([spec], spawn, clock)
    for _ in range(100):  # far past the start limit
        sup.tick()
        clock.t += 5
    assert spawn.names() == [] and sup.state("engine") == "waiting"
    taken[0] = False
    sup.tick()
    assert spawn.names() == ["engine"]


def test_engine_spec_checks_the_port():
    engine = supervisor.child_specs("windows", "zj.exe")[0]
    assert engine.served_elsewhere is supervisor.engine_port_taken


def test_engine_port_probe(monkeypatch):
    import socket

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        monkeypatch.setattr(supervisor, "ENGINE_PORT", srv.getsockname()[1])
        assert supervisor.engine_port_taken()
    assert not supervisor.engine_port_taken()  # closed now


def test_shutdown_quits_the_daemon_first_then_ends_the_rest():
    sup, clock, spawn = make()
    sup.tick()
    order = []
    daemon = spawn.last("daemon")

    def quit_daemon():
        order.append("quit")
        daemon.code = 0

    sup.shutdown(quit_daemon, clock.sleep)
    assert order == ["quit"]
    assert not daemon.terminated
    assert spawn.last("engine").terminated and spawn.last("tray").terminated
    sup.tick()
    assert spawn.names() == ["engine", "daemon", "tray"]  # nothing restarts after shutdown
    assert sup.state("daemon") == "stopped"


def test_shutdown_ends_a_daemon_that_does_not_quit_in_time():
    sup, clock, spawn = make()
    sup.tick()
    start = clock.t
    sup.shutdown(lambda: None, clock.sleep, timeout_s=60)
    assert spawn.last("daemon").terminated
    assert 60 <= clock.t - start < 70


def test_shutdown_survives_an_unreachable_daemon():
    sup, clock, spawn = make()
    sup.tick()

    def refused():
        raise ConnectionRefusedError

    sup.shutdown(refused, clock.sleep, timeout_s=1)
    assert spawn.last("daemon").terminated


def test_serve_ticks_until_asked_to_stop():
    sup, clock, spawn = make()
    checks = iter([False, False, True])
    supervisor.serve(sup, lambda: next(checks), clock.sleep, quit_daemon=lambda: None, poll_s=1)
    assert spawn.names() == ["engine", "daemon", "tray"]
    assert all(p.terminated for _, p in spawn.started)


# ---- real processes ----

def test_spawn_child_logs_and_drops_the_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    code = "import os; print(os.environ.get('ANTHROPIC_API_KEY'), os.environ['PYTHONUNBUFFERED'])"
    spec = ChildSpec("daemon", [sys.executable, "-c", code], Policy())
    p = supervisor.spawn_child(spec, tmp_path, "linux", cwd=tmp_path)
    for _ in range(100):
        if p.poll() is not None:
            break
        time.sleep(0.05)
    assert p.poll() == 0
    assert (tmp_path / "daemon.log").read_text(encoding="utf-8").strip() == "None 1"


def test_old_log_is_rotated(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "LOG_MAX_BYTES", 10)
    (tmp_path / "x.log").write_text("x" * 20, encoding="utf-8")
    supervisor._open_log(tmp_path / "x.log").close()
    assert (tmp_path / "x.log.1").read_text(encoding="utf-8") == "x" * 20
    assert (tmp_path / "x.log").read_text(encoding="utf-8") == ""


def test_kill_tree_uses_taskkill_without_a_window():
    seen = []

    class R:
        returncode = 0

    def run(argv, **kw):
        seen.append((argv, kw))
        return R()

    assert supervisor.kill_tree(42, run=run)
    (argv, kw), = seen
    assert argv == ["taskkill", "/PID", "42", "/T", "/F"]
    assert kw["creationflags"] == supervisor.CREATE_NO_WINDOW


# ---- single instance and stopping ----

def test_instance_lock_is_exclusive(tmp_path):
    a, b = InstanceLock(tmp_path / "l"), InstanceLock(tmp_path / "l")
    assert a.acquire()
    assert not b.acquire()
    assert supervisor.is_running(tmp_path / "l")
    a.release()
    assert not supervisor.is_running(tmp_path / "l")
    assert b.acquire()
    b.release()


@pytest.fixture
def win_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield tmp_path / "local" / "zundamon-jarvis"
    for h in root.handlers:  # main() points the root logger at its log file
        if h not in handlers:
            h.close()
    root.handlers[:], root.level = handlers, level


def test_second_supervisor_exits_at_once(win_dirs, monkeypatch):
    held = InstanceLock(supervisor.platform.runtime_dir("windows") / supervisor.LOCK)
    assert held.acquire()
    monkeypatch.setattr(supervisor, "child_specs", lambda *a: pytest.fail("must not start children"))
    try:
        assert supervisor.main([], system="windows") == supervisor.EXIT_ALREADY_RUNNING
    finally:
        held.release()
    assert (win_dirs / "logs" / "supervisor.log").exists()


def test_main_runs_until_the_stop_file_appears(win_dirs, monkeypatch):
    run = supervisor.platform.runtime_dir("windows")
    seen = {}

    def fake_serve(sup, should_stop, sleep, quit_daemon):
        seen["pid"] = (run / supervisor.PID).read_text(encoding="utf-8")
        seen["running"] = supervisor.running_pid("windows")
        assert not should_stop()
        (run / supervisor.STOP).write_text("", encoding="utf-8")
        assert should_stop()

    monkeypatch.setattr(supervisor, "serve", fake_serve)
    monkeypatch.setattr("zundamon_jarvis.service.find_exe", lambda s=None: "zj.exe")
    (run / supervisor.STOP).write_text("", encoding="utf-8")  # stale: must not stop the new supervisor at once
    assert supervisor.main([], system="windows") == 0
    assert seen == {"pid": str(os.getpid()), "running": os.getpid()}
    assert not (run / supervisor.PID).exists() and not (run / supervisor.STOP).exists()
    assert supervisor.running_pid("windows") is None


def test_stop_when_nothing_runs_still_quits_a_stray_daemon(win_dirs):
    asked = []
    assert supervisor.stop("windows", quit_daemon=lambda: asked.append(1)) == "not running"
    assert asked == [1]


def test_stop_asks_and_waits_for_the_supervisor(win_dirs):
    run = supervisor.platform.runtime_dir("windows")
    held = InstanceLock(run / supervisor.LOCK)
    assert held.acquire()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        assert (run / supervisor.STOP).exists()
        if len(sleeps) == 3:
            held.release()  # the supervisor saw the stop file and exited

    clock = Clock()
    r = supervisor.stop("windows", quit_daemon=lambda: None, kill=lambda pid: pytest.fail("no kill"),
                        clock=clock, sleep=sleep)
    assert r == "stopped" and len(sleeps) == 3


def test_stop_kills_a_supervisor_that_hangs(win_dirs):
    run = supervisor.platform.runtime_dir("windows")
    held = InstanceLock(run / supervisor.LOCK)
    assert held.acquire()
    (run / supervisor.PID).write_text("4321", encoding="utf-8")
    killed = []
    clock = Clock()

    def kill(pid):
        killed.append(pid)
        held.release()

    r = supervisor.stop("windows", quit_daemon=lambda: None, kill=kill, clock=clock, sleep=clock.sleep,
                        timeout_s=75)
    assert r == "killed" and killed == [4321]
    assert not (run / supervisor.STOP).exists()


def test_usage(win_dirs):
    assert supervisor.main(["extra"], system="windows") == 2
