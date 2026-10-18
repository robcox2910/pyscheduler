"""Start job attempts and find out how they ended.

The engine hands a ``Launcher`` a ``LaunchSpec`` (what to run, where the
logs and the status note go) and gets back an ``AttemptHandle``. It can
``wait()`` on the handle for an ``AttemptResult``, or ``terminate()`` /
``kill()`` the attempt's whole process group.

``MinderLauncher`` is the real one: it runs each attempt under the minder
(``python -I -m pyscheduler.minder``), which writes a status note when the
job ends. Tests use ``pyscheduler.testing.FakeLauncher`` instead.
"""

import contextlib
import functools
import json
import logging
import signal
import subprocess
import sys
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pynet.loop
from pynet.loop import EventLoop, Future, WorkerPool, get_child_watcher, get_running_loop
from pynet.process import Process

from pyscheduler.minder import Note, utc_now, write_status

CORRUPT_PREFIX = "corrupt status file: "

logger = logging.getLogger("pyscheduler.engine.launcher")

_LIVE_MINDERS: set[int] = set()

LAUNCH_WORKERS = 1
"""How many helper threads start minders (their own team, apart from the store's).

One is enough and more is worse: ``fork`` holds Python's GIL while it
copies the scheduler, so several launch threads forking at once keep the
loop's own thread waiting for its turn. With one thread the loop always
gets the GIL back between two forks (measured: 200 launches at once, the
loop's worst lateness about 30 ms with one thread, 70-160 ms with four).
"""

_LAUNCH_POOL = WorkerPool(LAUNCH_WORKERS)
_HOOKED_LOOPS: weakref.WeakSet[EventLoop] = weakref.WeakSet()


def _launch_pool(loop: EventLoop) -> WorkerPool:
    """Return the launch threads, telling ``loop`` they count as real work in flight.

    They are a team of their own so a burst of launches never queues in
    front of the store writer's commits on the loop's shared helper pool.
    """
    if loop not in _HOOKED_LOOPS:
        _HOOKED_LOOPS.add(loop)
        loop.add_busy_check(_LAUNCH_POOL.busy)
    return _LAUNCH_POOL


def live_minder_pids() -> frozenset[int]:
    """Return the pids of the minders this process launched and is still waiting for.

    PyNet's child watcher owns those, so nothing else may ``waitpid`` them.
    """
    return frozenset(_LIVE_MINDERS)


@dataclass(frozen=True)
class LaunchSpec:
    """Everything needed to start one attempt of one run.

    Attributes:
        run_id: The run this attempt belongs to, ``{job}.{run_date}.{seq}``.
        attempt: The attempt number, starting at 1.
        argv: The command and its arguments.
        cwd: The working directory, or None for the scheduler's own.
        env: The job's whole environment.
        stdout_path: The log file for stdout.
        stderr_path: The log file for stderr, or None to share stdout's.
        status_path: Where the minder writes its status note.

    """

    run_id: str
    attempt: int
    argv: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str]
    stdout_path: Path
    stderr_path: Path | None
    status_path: Path


@dataclass(frozen=True)
class AttemptResult:
    """How one attempt ended.

    Attributes:
        exit_code: The job's exit code, or None if a signal ended it or it never started.
        signal: The signal number that ended it, or None.
        started_at: When the job started, or None if unknown.
        ended_at: When it ended (or when we found out).
        start_error: Why it could not start or be read, or None.

    """

    exit_code: int | None
    signal: int | None
    started_at: datetime | None
    ended_at: datetime
    start_error: str | None

    @property
    def succeeded(self) -> bool:
        """Return whether the job exited 0 with no signal and no start error."""
        return self.exit_code == 0 and self.signal is None and self.start_error is None


class AttemptHandle(Protocol):
    """A running (or finished) attempt."""

    @property
    def pid(self) -> int | None:
        """Return the attempt's process (and process group) id, if it has one."""
        ...

    async def wait(self) -> AttemptResult:
        """Wait for the attempt to end and return how it ended."""
        ...

    def terminate(self) -> None:
        """Ask the attempt's whole process group to stop (SIGTERM)."""
        ...

    def kill(self) -> None:
        """Stop the attempt's whole process group at once (SIGKILL)."""
        ...


class Launcher(Protocol):
    """Start attempts."""

    async def launch(self, spec: LaunchSpec) -> AttemptHandle:
        """Start the attempt described by ``spec`` and return its handle."""
        ...


def _instant(value: object) -> datetime:
    """Parse an ISO 8601 instant with a zone into aware UTC.

    Raises:
        ValueError: If the value is not a string or has no time zone.

    """
    if not isinstance(value, str):
        message = f"bad instant {value!r}"
        raise ValueError(message)  # noqa: TRY004 -- one error type for any bad note
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        message = f"instant without a time zone {value!r}"
        raise ValueError(message)
    return instant.astimezone(UTC)


def _optional_int(data: Mapping[str, object], key: str) -> int | None:
    """Return ``data[key]`` if it is an int or None (bools rejected)."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"bad {key} {value!r}"
        raise ValueError(message)  # noqa: TRY004 -- one error type for any bad note
    return value


def _parse_status(text: str) -> AttemptResult:
    """Turn the minder's JSON note into an ``AttemptResult``.

    Raises:
        ValueError: If the note is not the shape the minder writes.

    """
    raw: object = json.loads(text)
    if not isinstance(raw, dict):
        message = "not a JSON object"
        raise ValueError(message)  # noqa: TRY004 -- one error type for any bad note
    data = cast("dict[str, object]", raw)  # JSON object keys are always strings
    start_error = data.get("start_error")
    if start_error is not None and not isinstance(start_error, str):
        message = f"bad start_error {start_error!r}"
        raise ValueError(message)
    started_at = data.get("started_at")
    return AttemptResult(
        exit_code=_optional_int(data, "exit_code"),
        signal=_optional_int(data, "signal"),
        started_at=None if started_at is None else _instant(started_at),
        ended_at=_instant(data.get("ended_at")),
        start_error=start_error,
    )


def read_status(path: Path) -> AttemptResult | None:
    """Read the minder's status note at ``path``.

    Return None if there is no note. A note that exists but cannot be read
    or understood gives a result whose ``start_error`` begins
    ``corrupt status file:``; this never raises.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _corrupt(exc)
    try:
        return _parse_status(text)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError is a ValueError
        return _corrupt(exc)


def _corrupt(exc: Exception) -> AttemptResult:
    """Build the result for a status note that could not be understood."""
    return AttemptResult(
        exit_code=None,
        signal=None,
        started_at=None,
        ended_at=datetime.now(UTC),
        start_error=f"{CORRUPT_PREFIX}{exc}",
    )


def pid_path(status_path: Path) -> Path:
    """Return where the minder writes its pid, next to the status note."""
    return status_path.with_name(f"{status_path.name}.pid")


class MinderHandle:
    """An attempt running under the minder."""

    def __init__(self, process: Process, status_path: Path) -> None:
        """Wrap the minder ``process`` whose note goes to ``status_path``."""
        self._process = process
        self._status_path = status_path
        self._result: AttemptResult | None = None

    @property
    def pid(self) -> int:
        """Return the minder's pid, which is also the attempt's process group id."""
        return self._process.pid

    async def wait(self) -> AttemptResult:
        """Wait for the minder to exit, then read its note.

        With no note, the result comes from the minder's own exit status: a
        signal that killed it, or a ``start_error`` explaining that it
        exited without writing one. Later calls return the same result.
        The note is read on a helper thread: a file read is a blocking
        call, and two hundred endings at once would add up on the loop.
        """
        if self._result is not None:
            return self._result
        code = await self._process.wait()
        _LIVE_MINDERS.discard(self._process.pid)
        note = await pynet.loop.run_in_thread(read_status, self._status_path)
        if self._result is None:
            self._result = note or _synthesise(code)
        return self._result

    def terminate(self) -> None:
        """Send SIGTERM to the attempt's whole process group."""
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        """Send SIGKILL to the attempt's whole process group."""
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        """Signal the group; an empty group (everyone gone) is not an error."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            self._process.kill_group(sig)


def _synthesise(code: int) -> AttemptResult:
    """Describe an attempt whose minder left no note, from its exit status."""
    now = datetime.now(UTC)
    if code < 0:
        return AttemptResult(None, -code, None, now, None)
    return AttemptResult(code, None, None, now, f"minder exited {code} without writing a status")


class MinderLauncher:
    """Run each attempt under ``python -I -m pyscheduler.minder``.

    Args:
        python: The interpreter that runs the minder.

    """

    def __init__(self, python: str = sys.executable) -> None:
        """Remember which interpreter runs the minder."""
        self.python = python

    def command(self, spec: LaunchSpec) -> list[str]:
        """Return the minder command line for ``spec``."""
        argv = [self.python, "-I", "-m", "pyscheduler.minder"]
        argv += ["--status", str(spec.status_path), "--stdout", str(spec.stdout_path)]
        if spec.stderr_path is not None:
            argv += ["--stderr", str(spec.stderr_path)]
        return [*argv, "--", *spec.argv]

    async def launch(self, spec: LaunchSpec) -> MinderHandle:
        """Start the minder in a new session on a helper thread and return its handle.

        Starting a process is slow work for an event loop: ``fork`` and
        ``exec`` take a few milliseconds, and the pid file is fsynced. Two
        hundred launches in a row would freeze the loop for a second, so
        the whole start (removing any old note and pid file, the fork, and
        writing the ``.pid`` file) runs on one of the launch threads, and only the cheap hand-over to the child watcher runs on
        the loop.

        The ``.pid`` file is written by the scheduler as soon as the
        process exists, so a scheduler that dies right after the launch
        still leaves the pid behind for recovery (the minder writes the
        same file again when it starts). Only a crash between the fork and
        this write leaves no trace.

        If the caller is cancelled while the thread is still starting the
        minder, the minder is still handed to the child watcher (so it is
        reaped and tracked until it exits); nobody waits for its note, and
        recovery finds it on the next start.

        Raises:
            OSError: If the minder itself cannot be started.
            RuntimeError: If the loop's child watcher is not installed.

        """
        watcher = get_child_watcher()
        if not watcher.is_installed():
            message = "the child watcher is not installed"
            raise RuntimeError(message)
        loop = get_running_loop()
        started: Future[Process] = Future(loop)
        command = self.command(spec)

        def adopt(popen: subprocess.Popen[bytes] | None, error: BaseException | None) -> None:
            """On the loop: wrap the new process (tracking it) and hand it to the waiter."""
            if error is not None or popen is None:
                if not started.done():
                    started.set_exception(error or RuntimeError("the minder did not start"))
                return
            process = Process(popen, loop, stdin=None, stdout=None, stderr=None, new_session=True)
            _LIVE_MINDERS.add(process.pid)
            if started.done():  # the launch was cancelled: nobody will wait for this one
                pynet.loop.spawn(_forget_when_done(process), name=f"orphan-{spec.run_id}")
            else:
                started.set_result(process)

        def deliver(popen: subprocess.Popen[bytes] | None, error: BaseException | None) -> None:
            """On the helper thread: pass the outcome to the loop (if it is still open)."""
            with contextlib.suppress(RuntimeError):  # the loop has closed: nobody to tell
                loop.call_soon_threadsafe(functools.partial(adopt, popen, error))

        _launch_pool(loop).submit(functools.partial(_start_minder, command, spec), deliver)
        process = await started
        return MinderHandle(process, spec.status_path)


def _start_minder(command: list[str], spec: LaunchSpec) -> subprocess.Popen[bytes]:
    """Remove any old note and pid file, start the minder, and write its pid file (a thread).

    Raises:
        OSError: If the minder cannot be started.

    """
    for stale in (spec.status_path, pid_path(spec.status_path)):
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            stale.unlink()
    popen = subprocess.Popen(  # noqa: S603 -- running the job's command is the point
        command,
        cwd=spec.cwd,
        env=dict(spec.env),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    note: Note = {"pid": popen.pid, "started_at": utc_now()}
    try:
        write_status(pid_path(spec.status_path), note)
    except OSError:  # the minder writes it too; recovery copes without it
        logger.warning("cannot write the pid file of %s", spec.run_id, exc_info=True)
    return popen


async def _forget_when_done(process: Process) -> None:
    """Wait for an orphaned minder to exit, then stop counting it as live."""
    try:
        await process.wait()
    finally:
        _LIVE_MINDERS.discard(process.pid)
