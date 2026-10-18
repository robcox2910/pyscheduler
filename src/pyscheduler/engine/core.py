"""The scheduler engine: one waiter, many tables.

A restaurant has one waiter (PyNet's event loop) and many tables (the
running attempts). The waiter never stands at one table waiting for a
diner to finish: they take an order, move on, and come back when called.
The engine works the same way. It is a handful of tasks on one loop:

- the **firing task** sleeps until the next job is due, creates its
  runs, and goes back to sleep;
- one **attempt task** per running attempt waits for its process;
- the **store writer** commits changes in groups.

File watchers (``watch_runs``) are attempts too, but they hold no
global slot: their looks at the disk share ``watch_limit`` instead.

State lives in memory (the run book) and is mirrored to the store
write-ahead: a change is durable before it is shown or acted on.

The work is split over a few modules: ``base`` (shared state and the
write-ahead rule), ``creation`` (runs, dependencies, overlap),
``attempts`` (launching, timeouts, retries), ``actions`` (operator
actions on runs, with their shared steps in ``action_support`` and the
per-run locks in ``locks``), ``reload`` (trigger, backfill,
reload) and ``recovery`` (recovery, catch-up and retention). ``Engine``
puts them together with the firing task and the pump that starts
queued runs.
"""

import heapq
from datetime import datetime, timedelta

import pynet.loop
from pynet.loop import Cancelled, Semaphore, Task

from pyscheduler.config import Config
from pyscheduler.engine.base import EngineFailedError, logger
from pyscheduler.engine.clock import MAX_NAP, Clock
from pyscheduler.engine.launcher import Launcher, drain_launches
from pyscheduler.engine.model import RunState, Trigger
from pyscheduler.engine.recovery import LAST_TICK, Recovery
from pyscheduler.schedule.firings import Firing, next_firing
from pyscheduler.store import Store

__all__ = ["LAST_TICK", "Engine"]

LATE = timedelta(seconds=2 * MAX_NAP)
"""How far behind the wall clock a firing may be found before it counts as a clock jump."""

JUST_BEFORE = timedelta(microseconds=1)
"""How far before ``now`` the first firing search starts, so a firing due right now counts."""

PUMP_BATCH = 16
"""The most queued runs one pump starts; more wait for the next loop turn.

Starting an attempt is quick, but two hundred in one go would hold the
loop for tens of milliseconds. A batch per turn lets timers, endings and
API requests in between.
"""

type _Due = tuple[datetime, str, Firing]


class Engine(Recovery):
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
        self._firing_asleep = False
        self._firing_due: datetime | None = None
        self._firings_stale = False
        self._stopped = False
        self._pump_planned = False
        self._watch_size = config.settings.watch_limit
        self.watch_limit = Semaphore(self._watch_size)
        self._files = {}
        self._dropped = {}
        self.last_active = None
        self.watch_reloads(self._resize_watch_limit)

    async def start(self) -> None:
        """Start the writer, recover, catch up, then start the firing task and prune.

        After a restart the firing task starts strictly after the
        catch-up instant (catch-up has handled everything up to it); on
        the very first start a firing due right now still counts.

        Raises:
            EngineFailedError: If a recovery or catch-up commit failed (the
                engine has failed stop; ``error`` holds the cause).

        """
        await self.writer.start()
        await self.recover()
        now = self.clock.now()
        tick, record = await pynet.loop.run_in_thread(
            lambda: (self.store.get_engine(LAST_TICK), self.store.latest_config())
        )
        first = tick is None
        marks = [self.last_active, record and record.loaded_at]
        marks.append(None if tick is None else datetime.fromisoformat(tick))
        self.last_active = max((mark for mark in marks if mark is not None), default=None)
        await self.catch_up(now)
        self._firings_after = None if first else now
        self._firing_task = pynet.loop.spawn(self._fire_loop(), name="engine-firings")
        self._retention_due()  # prune once now; it plans the hourly rounds

    async def stop(self, *, kill_running: bool = False) -> None:
        """Stop creating and starting runs, flush the writer and close the store.

        Running attempts are left running (their minders carry on) unless
        ``kill_running``, in which case each is killed and recorded as
        killed. An attempt caught between its ``running`` commit and its
        launch is cancelled instead, so with ``kill_running`` a process
        that was mid-launch can still be left running; recovery finds it
        on the next start. ``stop`` never raises because of a task's
        error (those are logged), always closes the writer and the store,
        and does nothing when called again. Just before the writer closes
        it awaits the ``before_close`` listeners.
        """
        if self._stopped:
            return
        self._stopped = True
        self._stopping = True
        self.closing.set()  # the launch thread refuses everything still queued
        try:
            await self._wind_down(kill_running=kill_running)
            await drain_launches()  # after this, no minder is forked any more
        finally:
            try:
                await self._notify_closing()  # the last chance to save (the alert monitor)
            finally:
                await self._close_store()

    async def _close_store(self) -> None:
        """Close the writer (committing what is pending), then the store; log any error."""
        try:
            await self.writer.close()
        except Exception:  # noqa: BLE001 -- logged; stop must finish
            logger.exception("closing the store writer failed")
        finally:
            try:
                await pynet.loop.run_in_thread(self.store.close)
            except Exception:  # noqa: BLE001 -- logged; stop must finish
                logger.exception("closing the store failed")

    async def prune(self, now: datetime) -> int:
        """Prune as ``Retention.prune`` does, then forget the files of runs no longer in memory."""
        deleted = await super().prune(now)
        self._files = {k: v for k, v in self._files.items() if self.book.get(k) is not None}
        return deleted

    def fail_stop(self, exc: BaseException) -> None:
        """Fail the engine stop because a part of it (say, alert delivery) could not commit.

        Store failures are fail-stop wherever they happen: the error is
        recorded, ``wait_failed`` returns it, and no new work starts.
        """
        self._fail(exc, ())

    async def _wind_down(self, *, kill_running: bool) -> None:
        """Cancel the firing task and timers, then end or abandon the attempt tasks."""
        if self._firing_task is not None:
            self._firing_task.cancel()
            await _quietly(self._firing_task)
        if self._retention_timer is not None:
            self._retention_timer.cancel()
            self._retention_timer = None
        for timers in (self._retry_timers, self._kill_timers):
            for timer in timers.values():
                timer.cancel()
            timers.clear()
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
        # Flushes, including any the attempts above started. A finished task
        # leaves ``_tasks`` in a done callback that may not have run yet, so
        # only unfinished ones are awaited (awaiting a done one never yields).
        while pending := [task for task in self._tasks if not task.done()]:
            for task in pending:
                await _quietly(task)

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
        clock jumps forward (a firing is found more than ``LATE`` behind
        it), the job's ``catch_up`` policy is applied to the firings it
        passed over, as at start-up.
        """
        try:
            heap = self._first_firings()
            while not self._stopping:
                if self._firings_stale:  # a reload came while we were busy
                    self._firings_stale = False
                    heap = self._first_firings()
                if not heap:
                    return
                self._firing_asleep = True
                self._firing_due = heap[0][0]
                await self.clock.sleep_until(heap[0][0])
                # Not in a ``finally``: a task cancelled by a reload must not
                # touch the flag, which belongs to its replacement by then.
                self._firing_asleep = False
                if self._stopping:
                    return
                self._busy += 1
                try:
                    tick, jumped = self._fire_due(heap)
                    if jumped:
                        await self._create_missed(jumped)
                    if tick is not None:
                        await self._flush(engine={LAST_TICK: tick.isoformat()})
                finally:
                    self._busy -= 1
        except EngineFailedError:
            return  # already recorded by _fail
        except Exception as exc:  # noqa: BLE001 -- routed to the fail path
            self._fail(exc, ())

    def _restart_firings(self) -> None:
        """Rebuild the firing heap from now, for the config in force.

        A firing task asleep (or finished) is replaced by a new one; one
        busy creating runs, or woken for a firing due already, rebuilds its
        heap when it has handled it.
        """
        task = self._firing_task
        if task is None or self._stopping:
            return
        self._firings_after = None
        due = self._firing_due
        waiting = self._firing_asleep and (due is None or due > self.clock.now())
        if waiting or task.done():
            task.cancel()
            self._firing_task = pynet.loop.spawn(self._fire_loop(), name="engine-firings")
        else:
            self._firings_stale = True

    def _fire_due(self, heap: list[_Due]) -> tuple[datetime | None, list[tuple[str, Firing]]]:
        """Create the runs of every firing due now; return the latest instant handled.

        A firing more than ``LATE`` behind the clock means the clock
        jumped: that job's firings up to now follow its ``catch_up`` policy
        instead of all being created. Those are returned (as
        ``(job, firing)``) for the caller to create as ``catch_up`` runs
        once their dates are loaded.
        """
        now = self.clock.now()
        tick: datetime | None = None
        jumped: list[tuple[str, Firing]] = []
        while heap and heap[0][0] <= now:
            when, name, firing = heapq.heappop(heap)
            tick = max(tick or when, when)
            job = self.config.jobs.get(name)
            if job is None or not job.enabled:
                continue
            after = when
            if when < now - LATE:
                logger.warning(
                    "clock jump: %s was due at %s but it is %s; applying catch_up %s",
                    name,
                    when.isoformat(),
                    now.isoformat(),
                    job.catch_up.value,
                )
                jumped += [(name, missed) for missed in self._missed(job, when - JUST_BEFORE, now)]
                after = tick = now
            else:
                self._create_run(name, firing.run_date, when, Trigger.SCHEDULE)
            upcoming = next_firing(job, self.config.calendar_for(job), after)
            if upcoming is not None:
                heapq.heappush(heap, (upcoming.scheduled_at, name, upcoming))
        return tick, jumped

    # -- starting queued runs --------------------------------------------

    def _pump(self) -> None:
        """Start the queued runs that fit, best first, at most ``PUMP_BATCH`` of them.

        If the batch is used up, another pump is planned for the next loop
        turn, so a burst of startable runs is spread over several turns.
        """
        freed = True
        taken = 0
        while freed and not self._stopping:  # go again if a slot was handed straight back
            freed = False
            force = frozenset(self._force)
            batch = self.ready.take_startable(self.admission, force=force, limit=PUMP_BATCH - taken)
            taken += len(batch)
            if taken >= PUMP_BATCH:
                self._pump_soon()
            for queued in batch:
                self._force.discard(queued.run_id)
                self._held.add(queued.run_id)
                current = self.book.get(queued.run_id)
                if current is None or current.state is not RunState.QUEUED:
                    self._release(queued.run_id)
                    freed = True
                elif not self._start_attempt(current):
                    freed = True

    def _pump_soon(self) -> None:
        """Plan a pump on the next loop turn (at most one is planned at a time)."""
        if self._pump_planned:
            return
        self._pump_planned = True

        async def pump() -> None:
            """Pump once this task gets its turn."""
            self._pump_planned = False
            self._pump()

        self._spawn(pump(), "engine-pump")


async def _quietly(task: Task[None]) -> None:
    """Wait for ``task`` to end, ignoring its cancellation and logging any error."""
    try:
        await task
    except Cancelled:
        pass
    except Exception:  # noqa: BLE001 -- logged; stop must finish
        logger.exception("engine task %s failed", task.get_name())
