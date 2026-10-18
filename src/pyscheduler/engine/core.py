"""The scheduler engine: one waiter, many tables.

A restaurant has one waiter (PyNet's event loop) and many tables (the
running attempts). The waiter never stands at one table waiting for a
diner to finish: they take an order, move on, and come back when called.
The engine works the same way. It is a handful of tasks on one loop:

- the **firing task** sleeps until the next job is due, creates its
  runs, and goes back to sleep;
- one **attempt task** per running attempt waits for its process;
- the **store writer** commits changes in groups.

State lives in memory (the run book) and is mirrored to the store
write-ahead: a change is durable before it is shown or acted on.

The work is split over a few modules: ``base`` (shared state and the
write-ahead rule), ``creation`` (runs, dependencies, overlap) and
``attempts`` (launching, timeouts, retries). ``Engine`` puts them
together with the firing task and the pump that starts queued runs.
"""

import heapq
from datetime import datetime, timedelta

import pynet.loop
from pynet.loop import Cancelled, Task

from pyscheduler.config import Config
from pyscheduler.engine.attempts import AttemptRunner
from pyscheduler.engine.base import EngineFailedError, logger
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.creation import RunCreation
from pyscheduler.engine.launcher import Launcher
from pyscheduler.engine.model import RunState, Trigger
from pyscheduler.schedule.firings import Firing, next_firing
from pyscheduler.store import Store

LAST_TICK = "last_tick"
"""The engine key holding the instant of the latest firing handled."""

JUST_BEFORE = timedelta(microseconds=1)
"""How far before ``now`` the first firing search starts, so a firing due right now counts."""

type _Due = tuple[datetime, str, Firing]


class Engine(RunCreation, AttemptRunner):
    """Run jobs on time, in dependency order, within the slots, durably.

    Args:
        config: The config to run.
        store: The open store that mirrors the engine's state.
        clock: The clock to tell the time and set timers with.
        launcher: What starts attempts.
        config_version: The version of ``config`` recorded on runs.

    """

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        clock: Clock,
        launcher: Launcher,
        config_version: int = 1,
    ) -> None:
        """Prepare the engine; nothing happens until ``start``."""
        super().__init__(
            config, store, clock=clock, launcher=launcher, config_version=config_version
        )
        self._firing_task: Task[None] | None = None
        self._firings_after: datetime | None = None
        self._stopped = False

    async def start(self) -> None:
        """Start the store writer and the firing task."""
        await self.writer.start()
        self._firing_task = pynet.loop.spawn(self._fire_loop(), name="engine-firings")

    async def stop(self, *, kill_running: bool = False) -> None:
        """Stop creating and starting runs, flush the writer and close the store.

        Running attempts are left running (their minders carry on) unless
        ``kill_running``, in which case each is killed and recorded as
        killed. An attempt caught between its ``running`` commit and its
        launch is cancelled instead, so with ``kill_running`` a process
        that was mid-launch can still be left running; recovery finds it
        on the next start. ``stop`` never raises because of a task's
        error (those are logged), always closes the writer and the store,
        and does nothing when called again.
        """
        if self._stopped:
            return
        self._stopped = True
        self._stopping = True
        try:
            await self._wind_down(kill_running=kill_running)
        finally:
            try:
                await self.writer.close()
            except Exception:  # noqa: BLE001 -- logged; stop must finish
                logger.exception("closing the store writer failed")
            finally:
                try:
                    await pynet.loop.run_in_thread(self.store.close)
                except Exception:  # noqa: BLE001 -- logged; stop must finish
                    logger.exception("closing the store failed")

    async def _wind_down(self, *, kill_running: bool) -> None:
        """Cancel the firing task and timers, then end or abandon the attempt tasks."""
        if self._firing_task is not None:
            self._firing_task.cancel()
            await _quietly(self._firing_task)
        for timer in self._retry_timers.values():
            timer.cancel()
        self._retry_timers.clear()
        attempts = list(self._attempt_tasks.items())
        for run_id, task in attempts:
            handle = self._handles.get(run_id)
            if kill_running and handle is not None:
                self._kill_requested.add(run_id)
                handle.kill()
            else:  # left to its minder, or not launched yet: recovery sorts it out
                task.cancel()
        for _, task in attempts:
            await _quietly(task)
        while self._tasks:  # flushes, including any the attempts above started
            await _quietly(next(iter(self._tasks)))

    # -- the firing task -------------------------------------------------

    def _first_firings(self) -> list[_Due]:
        """Return a heap of the next firing of every enabled, scheduled job."""
        after = self._firings_after or self.clock.now() - JUST_BEFORE
        heap: list[_Due] = []
        for job in self.config.jobs.values():
            if not job.enabled or not job.schedules:
                continue
            firing = next_firing(job, self.config.calendar_for(job), after)
            if firing is not None:
                heap.append((firing.scheduled_at, job.name, firing))
        heapq.heapify(heap)
        return heap

    async def _fire_loop(self) -> None:
        """Sleep until the next firing, create its runs, and repeat.

        Any error fails the engine (see ``EngineBase._fail``). If the wall
        clock jumps forward, every firing passed over is created on wake,
        each once; the catch-up policy for missed firings (``catch_up``)
        is applied at start-up by recovery, not here.
        """
        try:
            heap = self._first_firings()
            while heap and not self._stopping:
                await self.clock.sleep_until(heap[0][0])
                if self._stopping:
                    return
                self._busy += 1
                try:
                    tick = self._fire_due(heap)
                    if tick is not None:
                        await self._flush(engine={LAST_TICK: tick.isoformat()})
                finally:
                    self._busy -= 1
        except EngineFailedError:
            return  # already recorded by _fail
        except Exception as exc:  # noqa: BLE001 -- routed to the fail path
            self._fail(exc, ())

    def _fire_due(self, heap: list[_Due]) -> datetime | None:
        """Create the runs of every firing due now; return the latest instant handled."""
        now = self.clock.now()
        tick: datetime | None = None
        while heap and heap[0][0] <= now:
            when, name, firing = heapq.heappop(heap)
            tick = when
            job = self.config.jobs.get(name)
            if job is None:
                continue
            self._create_run(name, firing.run_date, when, Trigger.SCHEDULE)
            upcoming = next_firing(job, self.config.calendar_for(job), when)
            if upcoming is not None:
                heapq.heappush(heap, (upcoming.scheduled_at, name, upcoming))
        return tick

    # -- starting queued runs --------------------------------------------

    def _pump(self) -> None:
        """Start every queued run that fits, best first."""
        freed = True
        while freed and not self._stopping:  # go again if a slot was handed straight back
            freed = False
            force = frozenset(self._force)
            for queued in self.ready.take_startable(self.admission, force=force):
                self._force.discard(queued.run_id)
                self._held.add(queued.run_id)
                current = self.book.get(queued.run_id)
                if current is None or current.state is not RunState.QUEUED:
                    self._release(queued.run_id)
                    freed = True
                elif not self._start_attempt(current):
                    freed = True


async def _quietly(task: Task[None]) -> None:
    """Wait for ``task`` to end, ignoring its cancellation and logging any error."""
    try:
        await task
    except Cancelled:
        pass
    except Exception:  # noqa: BLE001 -- logged; stop must finish
        logger.exception("engine task %s failed", task.get_name())
