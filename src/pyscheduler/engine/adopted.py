"""Watch an attempt the scheduler did not start this time: an adopted minder.

When the scheduler restarts, a minder it launched earlier may still be
looking after its job. The minder is no longer the new scheduler's child,
so it cannot be waited for. Instead the scheduler peeks at it every few
seconds (``recovery.ADOPT_POLL``), like a parent checking the
babysitter's notes: is there a status note yet? Is the babysitter still
here? A note means the job ended, and how. No note and nobody home means
the attempt was lost.

"Is it still the same babysitter?" matters: when a process ends, the
system may give its pid to a brand-new process. So a pid only counts as
the minder (``minder_alive``) if it can be signalled by us (a minder
always runs as our user) *and* the process started within
``ADOPT_START_TOLERANCE`` of the ``started_at`` in the minder's ``.pid``
file, read with ``ps -o lstart=``. Where ``ps`` cannot be used, the pid
check alone is trusted (with a warning). A pid that is our own child
(a minder started earlier by this same process) is checked with
``waitpid``, which also clears it away once it has ended.
"""

import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pynet.loop import Future, get_running_loop

from pyscheduler.engine import launcher
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.launcher import AttemptResult, read_status

ADOPT_START_TOLERANCE = timedelta(seconds=10)
"""How far a process's start time may be from the minder's ``started_at`` and still be it."""

PS_TIMEOUT = 5.0
"""How long (in seconds) ``ps`` may take to report a start time."""

LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
"""The layout of ``ps -o lstart=`` in the C locale, such as ``Sun Oct 18 10:23:45 2026``."""

logger = logging.getLogger("pyscheduler.engine.adopted")


@dataclass(frozen=True)
class PidNote:
    """What the minder's ``.pid`` file says: its pid and when it started."""

    pid: int
    started_at: datetime | None


def read_pid(status_path: Path) -> PidNote | None:
    """Return the minder's ``.pid`` file next to ``status_path``, if it is readable."""
    try:
        raw: object = json.loads(launcher.pid_path(status_path).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, object]", raw)
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    started: datetime | None = None
    text = data.get("started_at")
    if isinstance(text, str):
        with contextlib.suppress(ValueError):
            started = datetime.fromisoformat(text)
    if started is not None and started.tzinfo is None:
        started = None
    return PidNote(pid, started)


def _own_child(pid: int) -> bool | None:
    """Return whether our own child ``pid`` still runs (reaping it if not); ``None`` if not ours."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return None
    return done == 0


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` is a live process of ours (cheap: no start-time check).

    A pid of 0 or less never is. A process we may not signal belongs to
    another user, so it cannot be a minder: it counts as gone.
    """
    if pid <= 0:
        return False
    child = _own_child(pid)
    if child is not None:
        return child
    try:
        os.kill(pid, 0)
    except ProcessLookupError, PermissionError:
        return False
    return True


def process_start(pid: int) -> datetime | None:
    """Return when ``pid`` started (from ``ps``), or ``None`` if there is no such process.

    Raises:
        OSError: If ``ps`` cannot be run or its answer cannot be read.

    """
    env = {**os.environ, "LC_ALL": "C"}
    try:
        done = subprocess.run(  # noqa: S603 -- a fixed command
            ["ps", "-o", "lstart=", "-p", str(pid)],  # noqa: S607 -- ps from PATH
            capture_output=True,
            text=True,
            env=env,
            timeout=PS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = "ps timed out"
        raise OSError(msg) from exc
    text = " ".join(done.stdout.split())
    if done.returncode != 0 or not text:
        return None
    try:
        parsed = time.strptime(text, LSTART_FORMAT)
    except ValueError as exc:
        msg = f"unreadable ps start time {text!r}"
        raise OSError(msg) from exc
    return datetime.fromtimestamp(time.mktime(parsed), UTC)


def minder_alive(pid: int, started_at: datetime) -> bool:
    """Return whether ``pid`` is still the minder that started at ``started_at``.

    Our own child is known for sure (``waitpid``). Any other pid must be
    ours to signal and have started within ``ADOPT_START_TOLERANCE`` of
    ``started_at``; if ``ps`` cannot tell, the pid check alone is trusted
    and a warning is logged.
    """
    if pid <= 0:
        return False
    child = _own_child(pid)
    if child is not None:
        return child
    if not pid_alive(pid):
        return False
    try:
        began = process_start(pid)
    except OSError as exc:
        logger.warning("cannot read the start time of pid %d (%s); trusting the pid", pid, exc)
        return True
    return began is not None and abs(began - started_at) <= ADOPT_START_TOLERANCE


class AdoptedHandle:
    """An attempt whose minder outlived the scheduler that started it.

    Attributes:
        lost: True once the attempt has ended with no status note.

    """

    def __init__(
        self, clock: Clock, pid: int, started_at: datetime, status_path: Path, *, poll: float
    ) -> None:
        """Watch the minder ``pid`` (started at ``started_at``) every ``poll`` seconds."""
        self._clock = clock
        self._started_at = started_at
        self._poll = timedelta(seconds=poll)
        self._pid = pid
        self._status_path = status_path
        self._killed = False
        self._waker: Future[None] | None = None
        self.lost = False

    @property
    def pid(self) -> int:
        """Return the minder's pid, which is also its process group id."""
        return self._pid

    def look(self) -> AttemptResult | None:
        """Return how the attempt ended, or ``None`` while the minder still runs.

        The note is read again after the pid is found gone, because the
        minder writes its note just before it exits.
        """
        result = read_status(self._status_path)
        if result is not None or pid_alive(self._pid):
            return result
        result = read_status(self._status_path)
        if result is None:
            self.lost = True
            result = AttemptResult(None, None, None, self._clock.now(), None)
        return result

    async def wait(self) -> AttemptResult:
        """Look every ``poll`` seconds (on the clock) until the attempt has ended.

        Once ``kill`` has sent SIGKILL (which cannot be caught), the
        attempt counts as ended at once, killed by that signal.
        """
        while (result := self._ended()) is None:
            waker: Future[None] = Future(get_running_loop())
            self._waker = waker
            timer = self._clock.call_at(self._clock.now() + self._poll, self._wake)
            try:
                await waker
            finally:
                timer.cancel()
                self._waker = None
        return result

    def _ended(self) -> AttemptResult | None:
        """Return the ending found by ``look``, or a SIGKILL ending after ``kill``."""
        result = self.look()
        if result is None and self._killed:
            result = AttemptResult(None, int(signal.SIGKILL), None, self._clock.now(), None)
        return result

    def _wake(self) -> None:
        """Wake ``wait`` for another look."""
        if self._waker is not None and not self._waker.done():
            self._waker.set_result(None)

    def terminate(self) -> None:
        """Send SIGTERM to the minder's process group (it leads its own session)."""
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        """Send SIGKILL to the minder's process group; ``wait`` then returns at once."""
        self._signal_group(signal.SIGKILL)
        self._killed = True
        self._wake()

    def _signal_group(self, sig: int) -> None:
        """Signal the group, but only if the pid is still our minder (never a stranger).

        One that has already gone is not an error.
        """
        if not minder_alive(self._pid, self._started_at):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pid, sig)
