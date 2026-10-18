"""Wake up after a nap: recovery, catch-up and retention.

When a parent comes home, they do not assume the house is as they left
it. They read the notes the babysitter left (the minders' status
notes), peek into rooms where someone may still be busy (live pids), see
which chores were missed while they were out (catch-up), and throw away
very old notes (retention). The engine does the same every time it
starts:

1. **recover**: load the open runs, and the final runs of the last
   ``memory_days``. A ``running`` run whose attempt left a status note is
   finished from it; one whose minder is still alive is *adopted* and
   watched until it ends; one with neither is ``lost`` (and may retry).
   Queued runs are queued again, retry waits re-armed, and the rest
   re-checked.
2. **catch_up**: apply each job's ``catch_up`` policy to the firings
   missed between ``last_tick`` and now.
3. Then, every hour, **prune**: delete final runs older than
   ``retention`` from the store and forget old ones in memory.

A run is never started twice: an attempt is only ever finished from its
note, adopted, or lost, and runs are unique by ``(job, scheduled_at)``.
"""

import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pynet.loop

from pyscheduler.config import CatchUp, JobDef
from pyscheduler.engine.adopted import AdoptedHandle, pid_alive, read_pid
from pyscheduler.engine.attempts import AttemptRunner, Watch
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.launcher import AttemptResult, read_status
from pyscheduler.engine.model import Attempt, Run, RunState, Trigger
from pyscheduler.engine.reload import DISABLED, ConfigReload
from pyscheduler.schedule.firings import Firing, firings_between
from pyscheduler.store import Batch, Store

ADOPT_POLL = 2.0
"""How often (in seconds) an adopted attempt's status note and pid are looked at."""

MAX_CATCH_UP = 1000
"""The most missed firings one job catches up with ``catch_up: all`` (the latest are kept)."""

PRUNE_BATCH = 500
"""How many old runs one pruning commit deletes."""

RETENTION_EVERY = timedelta(hours=1)
"""How often old history is pruned."""

LAST_TICK = "last_tick"
"""The engine key holding the instant of the latest firing handled."""

GONE = "job no longer configured"
"""The reason on an open run cancelled at start-up because its job is gone."""

logger = logging.getLogger("pyscheduler.engine.recovery")


@dataclass(frozen=True)
class _Found:
    """What recovery found about one running run's latest attempt."""

    row: Attempt | None
    status: AttemptResult | None
    pid: int | None


class Recovery(ConfigReload, AttemptRunner):
    """Give the engine recovery, catch-up and retention (see the module docstring)."""

    # -- recovery ----------------------------------------------------------

    async def recover(self) -> None:
        """Load the store's runs into memory and pick every open run up again.

        Every store read happens first (an open run older than the memory
        window brings back its date's final runs, so its dependencies can
        be checked). Then all open runs are placed in one step (slots for
        running attempts, the ready queue, retry timers, re-evaluation),
        committed in one flush, and only then are queued runs started and
        attempts finished, adopted or lost.
        """
        store = self.store
        newest = self._memory_start()
        open_runs = await pynet.loop.run_in_thread(store.load_open_runs)
        finals = await pynet.loop.run_in_thread(store.load_final_runs_since, newest)
        for run in [*finals, *open_runs]:
            self.book.put(run)
            self.book.publish(run)
        old_days: dict[str, set[date]] = {}
        for run in open_runs:
            if run.run_date < newest:  # its upstream runs may be older than memory too
                old_days.setdefault(run.job, set()).add(run.run_date)
        for name, days in sorted(old_days.items()):
            await self._load_dates(name, sorted(days), force=True)
        waiting = [r for r in open_runs if r.state in {RunState.RUNNING, RunState.RETRY_WAIT}]
        found = await pynet.loop.run_in_thread(lambda: [_inspect(store, r) for r in waiting])
        rows = {run.run_id: info for run, info in zip(waiting, found, strict=True)}
        for run in sorted(open_runs, key=lambda r: (r.scheduled_at, r.run_id)):
            job = self.config.jobs.get(run.job)
            if run.state is RunState.RUNNING:
                self._pick_up(run, job, rows[run.run_id])
            elif job is None or not job.enabled:
                reason = GONE if job is None else DISABLED
                self._stage(replace(self._changed(run, RunState.CANCELLED, reason), held_from=None))
            elif run.state is RunState.QUEUED:
                self._needs[run.run_id] = dict(job.pools)
                self.ready.push(run, job.criticality, job.pools)
            elif run.state is RunState.RETRY_WAIT:
                row = rows[run.run_id].row
                ended = (row.ended_at if row else None) or run.updated_at
                when = ended + job.retries.delay_for(run.attempt - run.rerun)
                self._retry_timers[run.run_id] = self.clock.call_at(when, self._retry_soon(run))
        self._reevaluate(sorted(self.config.jobs))
        await self._flush()

    def _retry_soon(self, run: Run) -> Callable[[], None]:
        """Return the guarded timer callback that retries ``run``."""
        run_id = run.run_id
        return self._guard(lambda: self._retry(run_id))

    def _pick_up(self, run: Run, job: JobDef | None, found: _Found) -> None:
        """Take a running run's slots, then finish, adopt or lose its attempt in a task.

        The slots are taken whatever the limits say: the process may still
        be running, and it counts until it ends.
        """
        run_id = run.run_id
        needs = dict(job.pools) if job is not None else {}
        self._needs[run_id] = needs
        self.admission.adopt(needs)
        self._held.add(run_id)
        row = found.row or self._lost_row(run)
        if found.status is not None:
            self._spawn(self._finish(row, found.status, Watch()), f"recover-{run_id}")
        elif found.row is not None and found.pid is not None and pid_alive(found.pid):
            task = self._spawn(self._adopt(run, found.row, found.pid), f"adopt-{run_id}")
            self._attempt_tasks[run_id] = task
        else:
            lost = AttemptResult(None, None, None, self.clock.now(), None)
            self._spawn(self._finish(row, lost, Watch(lost=True)), f"recover-{run_id}")

    def _lost_row(self, run: Run) -> Attempt:
        """Return an attempt row for a running run whose own row never became durable."""
        return Attempt(
            run_id=run.run_id,
            attempt=max(run.attempt, 1),
            started_at=run.updated_at,
            ended_at=None,
            exit_code=None,
            signal=None,
            outcome=None,
            pid=None,
            stdout_path="",
            stderr_path="",
            status_path=str(self._status_path(run.run_id, max(run.attempt, 1))),
            reason="",
        )

    async def _adopt(self, run: Run, row: Attempt, pid: int) -> None:
        """Watch a minder that outlived the last scheduler until its attempt ends.

        Its timeout counts from the attempt's real start, and an operator
        kill signals its process group, just as for an attempt started now.
        """
        handle = AdoptedHandle(self.clock, pid, Path(row.status_path), poll=ADOPT_POLL)
        self._handles[run.run_id] = handle
        row = replace(row, pid=pid)  # the pid may only have reached the minder's .pid file
        adopted = self._event(run.run_id, "attempt_adopted", attempt=row.attempt, pid=pid)
        try:
            await self._transition([], attempts=[row], events=[adopted])
        except EngineFailedError:
            self._handles.pop(run.run_id, None)
            raise
        watch = self._arm(run, handle, row.started_at)
        try:
            result = await self._idle_while(handle)
        finally:
            watch.cancel()
            self._handles.pop(run.run_id, None)
        watch.lost = handle.lost
        await self._finish(row, result, watch)

    # -- catch-up ----------------------------------------------------------

    async def catch_up(self, now: datetime) -> None:
        """Create the runs of the firings missed in ``(last_tick, now]``, per ``catch_up``.

        With no ``last_tick`` (the very first start) nothing is caught up.
        Either way ``last_tick`` becomes ``now``.
        """
        stored = await pynet.loop.run_in_thread(self.store.get_engine, LAST_TICK)
        if stored is not None:
            after = datetime.fromisoformat(stored)
            plan = [
                (job.name, firing)
                for job in sorted(self.config.jobs.values(), key=lambda j: j.name)
                if job.enabled and job.schedules
                for firing in self._missed(job, after, now)
            ]
            await self._prepare([(name, firing.run_date) for name, firing in plan])
            for name, firing in plan:
                if name in self.config.jobs:
                    self._create_run(name, firing.run_date, firing.scheduled_at, Trigger.CATCH_UP)
        await self._flush(engine={LAST_TICK: now.isoformat()})

    async def _prepare(self, slots: Iterable[tuple[str, date]]) -> None:
        """Load the old dates catch-up runs land on, so uniqueness and numbering hold."""
        newest = self._memory_start()
        days: dict[str, list[date]] = {}
        for name, day in slots:
            days.setdefault(name, []).append(day)
        for name, wanted in days.items():
            old = sorted({day for day in wanted if day < newest})
            if old:
                await self._load_dates(name, old, force=True)
            await self._seed_old_seqs(name, wanted)

    def _missed(self, job: JobDef, after: datetime, until: datetime) -> list[Firing]:
        """Return the firings of ``job`` in ``(after, until]`` that its policy catches up."""
        if job.catch_up is CatchUp.NONE:
            return []
        keep = 1 if job.catch_up is CatchUp.LATEST else MAX_CATCH_UP
        kept: deque[Firing] = deque(maxlen=keep)
        total = 0
        for firing in firings_between(job, self.config.calendar_for(job), after, until):
            kept.append(firing)
            total += 1
        if job.catch_up is CatchUp.ALL and total > keep:
            logger.warning(
                "job %s missed %d firings; catching up only the latest %d", job.name, total, keep
            )
        return list(kept)

    # -- retention ---------------------------------------------------------

    async def prune(self, now: datetime) -> int:
        """Delete final runs dated before ``now - retention``; forget old ones in memory.

        Runs go in batches of ``PRUNE_BATCH``, each with its attempts and
        events, in one commit. A run whose lock is held (an action is on
        it) or that memory has reopened is left for the next round. Open
        runs are never pruned, however old. Return how many were deleted.
        """
        cutoff = (now - self.config.settings.retention).date()
        deleted = 0
        while not self._stopping:
            ids = await pynet.loop.run_in_thread(
                self.store.final_run_ids_before, cutoff, PRUNE_BATCH
            )
            locked = [run_id for run_id in ids if self._prunable(run_id)]
            try:
                if locked:
                    await self._delete(locked)
            finally:
                for run_id in locked:
                    self._run_locks.give_back(run_id)
            deleted += len(locked)
            if len(ids) < PRUNE_BATCH or not locked:
                break
        self.book.evict_final_before(self._memory_start(now))
        return deleted

    def _prunable(self, run_id: str) -> bool:
        """Lock ``run_id`` if it may be deleted now (final in memory too, and not busy)."""
        run = self.book.get(run_id)
        if run is not None and not run.is_final:
            return False
        return self._run_locks.try_take(run_id)

    async def _delete(self, run_ids: list[str]) -> None:
        """Delete ``run_ids`` durably, then forget them (a failed commit fails the engine)."""
        try:
            await self.writer.save(Batch(deleted_run_ids=run_ids))
        except Exception as exc:
            self._fail(exc, ())
            raise EngineFailedError(str(exc)) from exc
        for run_id in run_ids:
            self.book.drop(run_id)

    def _arm_retention(self) -> None:
        """Plan the next pruning round ``RETENTION_EVERY`` from now (unless stopping)."""
        if self._stopping:
            return
        when = self.clock.now() + RETENTION_EVERY
        self._retention_timer = self.clock.call_at(when, self._guard(self._retention_due))

    def _retention_due(self) -> None:
        """Start a pruning round in its own task."""
        self._retention_timer = None
        self._spawn(self._retention_round(), "engine-retention")

    async def _retention_round(self) -> None:
        """Prune, then plan the next round."""
        try:
            await self.prune(self.clock.now())
        finally:
            self._arm_retention()


def _inspect(store: Store, run: Run) -> _Found:
    """Read a running or retry-waiting run's latest attempt, its note and its pid (blocking)."""
    attempts = store.attempts_for(run.run_id)
    row = next((a for a in attempts if a.attempt == run.attempt), None)
    if row is None or run.state is not RunState.RUNNING:
        return _Found(row, None, None)
    path = Path(row.status_path)
    return _Found(row, read_status(path), row.pid or read_pid(path))


async def recover(engine: Recovery) -> None:
    """Load the store and pick every open run up again (see ``Recovery.recover``)."""
    await engine.recover()


async def catch_up(engine: Recovery, now: datetime) -> None:
    """Create the runs of missed firings per policy (see ``Recovery.catch_up``)."""
    await engine.catch_up(now)


async def prune(engine: Recovery, now: datetime) -> int:
    """Delete old final runs and forget old ones in memory (see ``Recovery.prune``)."""
    return await engine.prune(now)
