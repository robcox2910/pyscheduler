"""Watch an attempt the scheduler did not start this time: an adopted minder.

When the scheduler restarts, a minder it launched earlier may still be
looking after its job. The minder is no longer the new scheduler's child,
so it cannot be waited for. Instead the scheduler peeks at it every
few seconds (``recovery.ADOPT_POLL``), like a parent checking the babysitter's notes:
is there a status note yet? Is the babysitter still here? A note means
the job ended, and how. No note and nobody home means the attempt was
lost.

Liveness is a plain pid check (``os.kill(pid, 0)``). If the minder died
and the system gave its pid to a new process, that process would look
like the minder until it exits; the risk is small and accepted.
"""

import contextlib
import json
import os
import signal
from datetime import timedelta
from pathlib import Path
from typing import cast

from pynet.loop import Future, get_running_loop

from pyscheduler.engine import launcher
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.launcher import AttemptResult, read_status


def pid_alive(pid: int) -> bool:
    """Return whether a process with ``pid`` exists (a pid of 0 or less never does)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it just belongs to someone else
    return True


def read_pid(status_path: Path) -> int | None:
    """Return the pid in the minder's ``.pid`` file next to ``status_path``, if readable."""
    try:
        raw: object = json.loads(launcher.pid_path(status_path).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    pid = cast("dict[str, object]", raw).get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


class AdoptedHandle:
    """An attempt whose minder outlived the scheduler that started it.

    Attributes:
        lost: True once the attempt has ended with no status note.

    """

    def __init__(self, clock: Clock, pid: int, status_path: Path, *, poll: float) -> None:
        """Watch the minder ``pid``, whose note goes to ``status_path``, every ``poll`` seconds."""
        self._clock = clock
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
        """Signal the group; one that has already gone is not an error."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pid, sig)
