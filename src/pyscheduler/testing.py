"""Stand-ins for tests: a launcher that runs no real processes.

``FakeLauncher`` pretends to start jobs. Each pretend attempt finishes when
the ``FakeClock`` reaches its start time plus a planned duration, with a
planned exit code or signal. A test can therefore run a day of jobs, with
failures, retries and kills, without starting a single process.
"""

import itertools
import signal
from dataclasses import dataclass
from datetime import timedelta

from pynet.loop import Future, get_running_loop

from pyscheduler.engine.clock import Cancel, FakeClock
from pyscheduler.engine.launcher import AttemptResult, LaunchSpec

FAKE_PID_BASE = 10_000
DEFAULT_DURATION = timedelta(minutes=1)


@dataclass(frozen=True)
class FakeResult:
    """How a pretend attempt should go.

    Attributes:
        duration: How long it runs on the fake clock.
        exit_code: The exit code it ends with; forced to None when ``signal`` is set.
        signal: The signal that ends it, if any.
        start_error: If set, it fails at launch with this message.
        ignores_term: If true, ``terminate()`` has no effect; only ``kill()`` stops it.

    """

    duration: timedelta = DEFAULT_DURATION
    exit_code: int | None = 0
    signal: int | None = None
    start_error: str | None = None
    ignores_term: bool = False

    def __post_init__(self) -> None:
        """Drop the default exit code when a signal ends the attempt."""
        if self.signal is not None:
            object.__setattr__(self, "exit_code", None)


class FakeHandle:
    """A pretend attempt that ends when the fake clock says so."""

    def __init__(
        self, launcher: FakeLauncher, spec: LaunchSpec, result: FakeResult, pid: int
    ) -> None:
        """Start the pretend attempt now, on ``launcher``'s clock."""
        self._launcher = launcher
        self._clock = launcher.clock
        self.spec = spec
        self.plan = result
        self.pid: int | None = pid
        self.started_at = self._clock.now()
        self._future: Future[AttemptResult] = Future(get_running_loop())
        self._timer: Cancel | None = None
        if result.start_error is not None:
            self._finish(None, None, result.start_error)
        else:
            self._timer = self._clock.call_at(
                self.started_at + result.duration,
                lambda: self._finish(result.exit_code, result.signal),
            )

    @property
    def done(self) -> bool:
        """Return whether the attempt has ended."""
        return self._future.done()

    async def wait(self) -> AttemptResult:
        """Wait for the attempt to end and return how it ended."""
        return await self._future

    def terminate(self) -> None:
        """End the attempt with SIGTERM, unless it ignores TERM."""
        if not self.plan.ignores_term:
            self._finish(None, int(signal.SIGTERM))

    def kill(self) -> None:
        """End the attempt with SIGKILL."""
        self._finish(None, int(signal.SIGKILL))

    def _finish(self, exit_code: int | None, signum: int | None, error: str | None = None) -> None:
        """Record the ending (once) and leave the launcher's running list."""
        if self._future.done():
            return
        if self._timer is not None:
            self._timer.cancel()
        self._future.set_result(
            AttemptResult(exit_code, signum, self.started_at, self._clock.now(), error)
        )
        self._launcher.running.pop(self.spec.run_id, None)


class FakeLauncher:
    """Pretend to launch attempts, finishing them on a ``FakeClock``.

    Args:
        clock: The fake clock that decides when attempts end.

    Attributes:
        launched: Every spec launched, in order.
        running: The attempts still running, by run id.
        max_concurrent: The most attempts that were ever running at once.

    """

    def __init__(self, clock: FakeClock) -> None:
        """Start with no plans and nothing launched."""
        self.clock = clock
        self.launched: list[LaunchSpec] = []
        self.running: dict[str, FakeHandle] = {}
        self.max_concurrent = 0
        self._plans: dict[str, tuple[FakeResult, ...]] = {}
        self._launches: dict[str, int] = {}
        self._pids = itertools.count(FAKE_PID_BASE + 1)

    def plan(self, job: str, *results: FakeResult) -> None:
        """Set how ``job``'s attempts go: in order, with the last one repeating.

        A job with no plan succeeds after ``DEFAULT_DURATION``.
        """
        self._plans[job] = results

    async def launch(self, spec: LaunchSpec) -> FakeHandle:
        """Start a pretend attempt for ``spec``, following its job's plan."""
        job = spec.run_id.split(".", 1)[0]
        count = self._launches.get(job, 0)
        self._launches[job] = count + 1
        results = self._plans.get(job) or (FakeResult(),)
        result = results[min(count, len(results) - 1)]
        self.launched.append(spec)
        handle = FakeHandle(self, spec, result, next(self._pids))
        if not handle.done:
            self.running[spec.run_id] = handle
            self.max_concurrent = max(self.max_concurrent, len(self.running))
        return handle
