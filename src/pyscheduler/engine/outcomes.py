"""How an attempt ended: the flags gathered while it ran, and its outcome.

``Watch`` is the attempt's clipboard: the timers set for it, and what was
noticed while it ran (timed out, lost, the files a watcher found).
``classify`` reads the clipboard and the result and names the outcome.
"""

from dataclasses import dataclass

from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.engine.clock import Cancel
from pyscheduler.engine.launcher import AttemptHandle, AttemptResult
from pyscheduler.engine.model import Outcome
from pyscheduler.engine.watch_runs import give_up_reason
from pyscheduler.engine.watcher import WatcherHandle

__all__ = ["LOST", "Watch", "classify"]

LOST = "lost"
"""The reason on an attempt whose process vanished without leaving a status note."""


@dataclass
class Watch:
    """The timers and flags of one running attempt.

    Attributes:
        timed_out: The timeout rang (the attempt was asked to stop).
        lost: The process vanished without a status note (found by recovery), or
            the hub gave a remote attempt up as lost.
        term: The timer that rings the timeout.
        kill: The timer that sends SIGKILL after the grace.
        files: The files a watcher attempt found (``()`` for others).
        why: The reason to show for a timeout instead of ``"timeout"``.

    """

    timed_out: bool = False
    lost: bool = False
    term: Cancel | None = None
    kill: Cancel | None = None
    files: tuple[str, ...] = ()
    why: str = ""

    def note(self, handle: AttemptHandle) -> None:
        """Take what a watcher found or that it gave up, or that a remote attempt was lost."""
        if isinstance(handle, RemoteHandle):
            self.lost = self.lost or handle.lost
        elif isinstance(handle, WatcherHandle):
            self.files = handle.files
            if handle.gave_up:
                self.timed_out = True
                self.why = give_up_reason(handle)

    def cancel(self) -> None:
        """Call off both timers."""
        for timer in (self.term, self.kill):
            if timer is not None:
                timer.cancel()


def classify(
    result: AttemptResult, *, timed_out: bool, killed: bool, lost: bool = False
) -> tuple[Outcome, str]:
    """Return the outcome of an attempt and the reason to show for it.

    A ``lost`` attempt (no status note, process gone) is ``lost`` unless
    it was being timed out or killed, which explains its end better.
    """
    if result.start_error is not None:
        return Outcome.START_ERROR, f"start error: {result.start_error}"
    if result.succeeded:
        return Outcome.SUCCEEDED, ""
    flags = ((timed_out, Outcome.TIMEOUT, "timeout"), (killed, Outcome.KILLED, "killed"))
    for flag, outcome, reason in (*flags, (lost, Outcome.LOST, LOST)):
        if flag:
            return outcome, reason
    if result.signal is not None:
        return Outcome.FAILED, f"signal {result.signal}"
    return Outcome.FAILED, f"exit {result.exit_code}"
