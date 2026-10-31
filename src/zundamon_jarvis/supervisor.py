"""Windows' always-on services: ``zundamon-jarvis supervise``.

Linux has systemd user units and macOS has LaunchAgents to start the VOICEVOX engine, the daemon
and the menu bar at login and to bring them back when they die. A per-user Windows install (no
admin, no Windows service) has neither, so one long-lived, windowless process does their job:
the ``Run`` registry key (``zundamon_jarvis.service``) starts ``zundamon-jarvisw supervise`` at
login, and it starts and watches three children:

* ``engine``: the VOICEVOX engine (DirectML build, ``--use_gpu``); always restarted, like the
  macOS agent (``KeepAlive``) -- nothing but the supervisor is meant to stop it;
* ``daemon``: ``zundamon-jarvis daemon``, restarted the way ``zundamon-jarvis.service`` does it:
  on failure only (exit 0 is ``zundamon-jarvisctl quit``), never after exit 3 (another daemon
  holds the control endpoint: restarting won't help), 5 s apart, and at most 5 starts in 5 minutes
  (a crash loop gives up). Exit 4 (audio stuck) is a failure, so it comes back with a fresh audio stack;
* ``tray``: ``zundamon-jarvis tray``; "Quit tray" (exit 0) stays quit, a crash restarts it (the
  macOS menu bar's ``KeepAlive = {"SuccessfulExit": False}``).

The restart decisions are the pure ``Supervisor`` (the tests drive it with a fake clock and fake
processes); ``main`` wires it to real ones. Children get no console window (``CREATE_NO_WINDOW``:
a hidden console of their own, which the claude CLI started by the daemon shares, so no window
flashes) and append to ``<log_dir>/<name>.log``.

Single instance: ``supervisor.lock`` in ``platform.runtime_dir()`` stays locked for the
supervisor's life. The OS drops the lock when the process dies, so a crash leaves nothing stale
(a pid file alone could name a reused pid). ``supervisor.pid`` beside it names the process for
the last-resort kill; it is a separate file because Windows locks are mandatory (a locked byte
cannot even be read). A second ``supervise`` exits 3 at once.

Stopping (``stop``; ``zundamon-jarvis service stop|uninstall``, and ``install`` before it starts a
fresh one): there is no SIGTERM to send on Windows, so the stopper creates ``supervisor.stop``.
The supervisor sees it within a second, asks the daemon to ``quit`` (it closes the Claude session
cleanly), waits up to 60 s for it (``TimeoutStopSec`` in the unit), ends the engine and the tray,
and exits. If it is still there after that, the stopper kills its process tree.
"""

import logging
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal, Protocol

from zundamon_jarvis import platform

log = logging.getLogger(__name__)

EXIT_ALREADY_RUNNING = 3  # the daemon's code for the same thing
POLL_S = 1.0
STOP_TIMEOUT_S = 60.0  # the daemon's clean shutdown (TimeoutStopSec=60 in zundamon-jarvis.service)
KILL_GRACE_S = 5.0  # for the children ended without asking
STOPPER_SLACK_S = 15.0  # the stopper waits this much longer than the supervisor waits for the daemon
LOG_MAX_BYTES = 10 * 1024 * 1024  # one older generation (<name>.log.1) is kept
ENGINE_ARGS = ("--host", "127.0.0.1", "--port", "50021", "--use_gpu")

LOCK, PID, STOP = "supervisor.lock", "supervisor.pid", "supervisor.stop"

# subprocess only defines these on Windows
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Policy:
    """When a child is started again after it exits (systemd's ``Restart=`` family)."""

    restart: Literal["always", "on-failure"] = "on-failure"
    prevent: frozenset[int] = frozenset()  # RestartPreventExitStatus=
    delay_s: float = 5.0  # RestartSec=
    burst: int = 5  # StartLimitBurst=
    interval_s: float = 300.0  # StartLimitIntervalSec=

    def restarts_after(self, code: int) -> bool:
        if code in self.prevent:
            return False
        return self.restart == "always" or code != 0


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    policy: Policy


class Proc(Protocol):
    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...


@dataclass
class _Child:
    spec: ChildSpec
    proc: Proc | None = None
    starts: deque[float] = field(default_factory=deque)
    next_start: float = 0.0
    done: bool = False  # stays down: a clean exit, a prevented status, the start limit, or shutdown


class Supervisor:
    """Start the children and decide, on each ``tick``, which ones to start again.

    ``spawn(spec)`` starts a process (raising OSError if it cannot); ``clock`` is monotonic
    seconds. Nothing here blocks except ``shutdown``, which sleeps through ``sleep``.
    """

    def __init__(self, specs: Sequence[ChildSpec], spawn: Callable[[ChildSpec], Proc],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._children = [_Child(s) for s in specs]
        self._spawn = spawn
        self._clock = clock

    def state(self, name: str) -> str:
        """``running`` / ``waiting`` (to be restarted) / ``stopped`` (for good)."""
        c = self._child(name)
        if c.proc is not None:
            return "running"
        return "stopped" if c.done else "waiting"

    def proc(self, name: str) -> Proc | None:
        return self._child(name).proc

    def _child(self, name: str) -> _Child:
        return next(c for c in self._children if c.spec.name == name)

    def tick(self) -> None:
        now = self._clock()
        for c in self._children:
            if c.proc is not None:
                code = c.proc.poll()
                if code is None:
                    continue
                c.proc = None
                if c.spec.policy.restarts_after(code):
                    c.next_start = now + c.spec.policy.delay_s
                    log.warning("%s exited with %s; restarting in %.0f s", c.spec.name, code,
                                c.spec.policy.delay_s)
                else:
                    c.done = True
                    log.info("%s exited with %s; not restarting it", c.spec.name, code)
            if c.proc is None and not c.done and now >= c.next_start:
                self._start(c, now)

    def _start(self, c: _Child, now: float) -> None:
        p = c.spec.policy
        while c.starts and now - c.starts[0] >= p.interval_s:
            c.starts.popleft()
        if len(c.starts) >= p.burst:
            c.done = True
            log.error("%s started %d times in %.0f s; giving up (see %s.log; "
                      "`zundamon-jarvis service install` starts everything again)",
                      c.spec.name, len(c.starts), p.interval_s, c.spec.name)
            return
        c.starts.append(now)  # a start that fails counts too: a missing engine is a crash loop
        try:
            c.proc = self._spawn(c.spec)
        except OSError as e:
            c.next_start = now + p.delay_s
            log.error("cannot start %s (%s): %s", c.spec.name, c.spec.argv[0], e)
            return
        log.info("started %s (pid %s)", c.spec.name, c.proc.pid)

    def shutdown(self, quit_daemon: Callable[[], Any], sleep: Callable[[float], Any],
                 timeout_s: float = STOP_TIMEOUT_S, grace_s: float = KILL_GRACE_S) -> None:
        """Stop every child for good: the daemon cleanly first (``quit_daemon``), then the rest."""
        for c in self._children:
            c.done = True
        daemon = next((c for c in self._children if c.spec.name == "daemon"), None)
        if daemon is not None and self._alive([daemon]):
            try:
                quit_daemon()
            except (OSError, ValueError) as e:  # not answering: it is ended below like the others
                log.warning("could not ask the daemon to quit: %s", e)
            self._wait([daemon], timeout_s, sleep)
        for c in self._alive(self._children):
            log.info("ending %s (pid %s)", c.spec.name, c.proc.pid)  # type: ignore[union-attr]
            c.proc.terminate()  # type: ignore[union-attr]
        self._wait(self._children, grace_s, sleep)
        for c in self._children:
            c.proc = None

    @staticmethod
    def _alive(children: Iterable[_Child]) -> list[_Child]:
        return [c for c in children if c.proc is not None and c.proc.poll() is None]

    def _wait(self, children: list[_Child], timeout_s: float, sleep: Callable[[float], Any]) -> None:
        deadline = self._clock() + timeout_s
        while self._alive(children) and self._clock() < deadline:
            sleep(0.2)


def serve(sup: Supervisor, should_stop: Callable[[], bool], sleep: Callable[[float], Any],
          quit_daemon: Callable[[], Any], poll_s: float = POLL_S) -> None:
    """Tick until ``should_stop()``, then shut everything down."""
    try:
        while not should_stop():
            sup.tick()
            sleep(poll_s)
    except KeyboardInterrupt:  # run by hand in a console
        pass
    log.info("stopping")
    sup.shutdown(quit_daemon, sleep)


# ---- the children ----

def child_specs(system: str, exe: str) -> list[ChildSpec]:
    """``exe`` is the console ``zundamon-jarvis`` (its hidden console is shared by what it runs)."""
    from zundamon_jarvis import engine

    return [
        ChildSpec("engine", [str(engine.run_path(system)), *ENGINE_ARGS], Policy(restart="always")),
        ChildSpec("daemon", [exe, "daemon"], Policy(prevent=frozenset({EXIT_ALREADY_RUNNING}))),
        ChildSpec("tray", [exe, "tray"], Policy()),
    ]


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never bill the API: the claude CLI must use the subscription login
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"  # a log file is not a console: Python would pick cp932 and choke on emoji
    return env


def _open_log(path: Path) -> IO[bytes]:
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    return open(path, "ab")


def kill_tree(pid: int, run: Callable[..., Any] = subprocess.run) -> bool:
    """End ``pid`` and everything it started (``taskkill /T /F``); True if taskkill succeeded."""
    try:
        r = run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                creationflags=CREATE_NO_WINDOW)  # a console program: no window flash from the windowless side
    except OSError as e:
        log.warning("taskkill failed: %s", e)
        return False
    return getattr(r, "returncode", 1) == 0


class _Popen:
    """A started child. On Windows, ``terminate`` ends its whole tree: the daemon is uv's
    launcher with Python (and the claude CLI) under it, and TerminateProcess ends only the top."""

    def __init__(self, p: subprocess.Popen, system: str) -> None:
        self._p, self._system = p, system

    @property
    def pid(self) -> int:
        return self._p.pid

    def poll(self) -> int | None:
        return self._p.poll()

    def terminate(self) -> None:
        if self._system == "windows" and kill_tree(self._p.pid):
            return
        try:
            self._p.terminate()
        except OSError:  # already gone
            pass


def spawn_child(spec: ChildSpec, logs: Path, system: str, cwd: Path | None = None) -> Proc:
    out = _open_log(logs / f"{spec.name}.log")
    kw: dict[str, Any] = ({"creationflags": CREATE_NO_WINDOW} if system == "windows"
                          else {"start_new_session": True})
    try:
        p = subprocess.Popen(spec.argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=child_env(), cwd=str(cwd or Path.home()), **kw)
    finally:
        out.close()  # the child has its own handle
    return _Popen(p, system)


# ---- single instance, and stopping it from outside ----

class InstanceLock:
    """An exclusive, non-blocking lock on a file, held until ``release`` or process exit."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._f: IO[bytes] | None = None

    def acquire(self) -> bool:
        f = open(self.path, "a+b")
        try:
            _lock(f)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        if self._f is not None:
            self._f.close()  # closing drops the lock (flock and LockFile alike)
            self._f = None


def _lock(f: IO[bytes]) -> None:
    try:
        import msvcrt
    except ImportError:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]


def is_running(lock_path: Path) -> bool:
    probe = InstanceLock(lock_path)
    if probe.acquire():
        probe.release()
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def running_pid(system: str | None = None) -> int | None:
    """The running supervisor's pid (0 if it runs but its pid file is unreadable), or None."""
    run = platform.runtime_dir(system)
    if not is_running(run / LOCK):
        return None
    return _read_pid(run / PID) or 0


def _quit_daemon() -> None:
    from zundamon_jarvis.control import request

    request("quit", timeout=5.0)


def stop(system: str | None = None, *, quit_daemon: Callable[[], Any] = _quit_daemon,
         kill: Callable[[int], Any] = kill_tree, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], Any] = time.sleep,
         timeout_s: float = STOP_TIMEOUT_S + STOPPER_SLACK_S) -> str:
    """Stop the supervisor and what it runs: ``stopped``, ``killed`` or ``not running``.

    The daemon is asked to quit even with no supervisor: one left behind by a killed supervisor
    would keep the control endpoint, and every new daemon would exit 3 for good.
    """
    run = platform.runtime_dir(system)
    lock = run / LOCK
    running = is_running(lock)
    if running:
        (run / STOP).write_text("", encoding="utf-8")
    try:
        quit_daemon()  # the supervisor asks too; asking now saves it a poll
    except (OSError, ValueError):
        pass  # not running
    if not running:
        return "not running"
    deadline = clock() + timeout_s
    while clock() < deadline:
        if not is_running(lock):
            return "stopped"
        sleep(0.5)
    pid = _read_pid(run / PID)
    log.warning("the supervisor did not stop in %.0f s; killing pid %s", timeout_s, pid)
    if pid is not None:
        kill(pid)
    for _ in range(20):  # the lock goes when the process is gone
        if not is_running(lock):
            break
        sleep(0.5)
    (run / STOP).unlink(missing_ok=True)
    return "killed"


# ---- `zundamon-jarvis supervise` ----

def main(argv: list[str] | None = None, system: str | None = None) -> int:
    if argv:
        print("usage: zundamon-jarvis supervise", file=sys.stderr)
        return 2
    sys_ = system or platform.system()
    logs = platform.log_dir(sys_)
    logs.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(logs / "supervisor.log", encoding="utf-8")]
    if sys.stderr is not None:  # None under the windowless launcher
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    run = platform.runtime_dir(sys_)
    lock = InstanceLock(run / LOCK)
    if not lock.acquire():
        log.info("the supervisor is already running")
        return EXIT_ALREADY_RUNNING
    stop_file, pid_file = run / STOP, run / PID
    try:
        stop_file.unlink(missing_ok=True)  # left by a stopper that had to kill the last one
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        from zundamon_jarvis import service

        specs = child_specs(sys_, service.find_exe(sys_))
        sup = Supervisor(specs, spawn=lambda spec: spawn_child(spec, logs, sys_))
        log.info("supervisor started (pid %s)", os.getpid())
        serve(sup, should_stop=stop_file.exists, sleep=time.sleep, quit_daemon=_quit_daemon)
    finally:
        pid_file.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        lock.release()
    log.info("supervisor stopped")
    return 0
