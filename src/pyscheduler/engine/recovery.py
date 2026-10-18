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

import json
import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pynet.loop

from pyscheduler.config import CatchUp, JobDef
from pyscheduler.config.model import WatchSpec
from pyscheduler.engine.adopted import AdoptedHandle, minder_alive, read_pid
from pyscheduler.engine.attempts import AttemptRunner, Watch
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.launcher import AttemptResult, read_status
from pyscheduler.engine.model import Attempt, Run, RunState, Trigger
from pyscheduler.engine.reload import DISABLED, ConfigReload
from pyscheduler.engine.retention import Retention, prune
from pyscheduler.schedule.firings import Firing, firings_between
from pyscheduler.store import ConfigRecord, Store
from pyscheduler.yamlish import Map, YamlError, parse

__all__ = ["ADOPT_POLL", "LAST_TICK", "MAX_CATCH_UP", "Recovery", "catch_up", "prune", "recover"]

ADOPT_POLL = 2.0
"""How often (in seconds) an adopted attempt's status note and pid are looked at."""

MAX_CATCH_UP = 1000
"""The most missed firings one job catches up with ``catch_up: all`` (the latest are kept)."""

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
    alive: bool = False
    killer: str | None = None
    began: datetime | None = None


class Recovery(ConfigReload, AttemptRunner, Retention):
    """Give the engine recovery, catch-up and retention (see the module docstring)."""

    # -- recovery ----------------------------------------------------------

    async def recover(self) -> None:
        """Load the store's runs into memory and pick every open run up again.

        Every store read happens first (an open run older than the memory
        window brings back its date's final runs, so its dependencies can
        be checked). Then all open runs are placed in one step (slots for
        running attempts, the ready queue, retry timers, re-evaluation),
        committed in one flush, and only then are queued runs started and
        attempts finished, adopted or lost. A file watcher that was polling
        has no process to find: it resumes, its give-up deadline still
        counted from its first start.
        """
        store = self.store
        newest = self._memory_start()
        open_runs = await pynet.loop.run_in_thread(store.load_open_runs)
        finals = await pynet.loop.run_in_thread(store.load_final_runs_since, newest)
        for run in [*finals, *open_runs]:
            self.book.put(run)
            self.book.publish(run)
        found_ids = [run.run_id for run in finals if run.state is RunState.SUCCEEDED]
        self._files = await pynet.loop.run_in_thread(store.run_files, found_ids)
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
                self._enqueue(run, job)
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
        spec = job.watch if job is not None else None
        watcher = found.row if found.row is not None and not found.row.status_path else None
        self.admission.adopt(needs, global_slot=watcher is None)
        if watcher is not None:
            self._slotless.add(run_id)
        self._held.add(run_id)
        if found.killer is not None:  # an operator kill was recorded: it still counts
            self._kill_requested.add(run_id)
            self._kill_actors[run_id] = found.killer
        row = found.row or self._lost_row(run)
        if job is not None and spec is not None and watcher is not None:
            task = self._spawn(self._resume_watch(run, job, spec, watcher), f"watch-{run_id}")
            self._attempt_tasks[run_id] = task
        elif found.status is not None:
            self._spawn(self._finish(row, found.status, Watch()), f"recover-{run_id}")
        elif found.row is not None and found.pid is not None and found.alive:
            began = found.began or found.row.started_at
            adopt = self._adopt(run, found.row, found.pid, began)
            task = self._spawn(adopt, f"adopt-{run_id}")
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

    async def _resume_watch(self, run: Run, job: JobDef, spec: WatchSpec, row: Attempt) -> None:
        """Poll again for a watcher attempt that was running when the scheduler stopped.

        Its give-up deadline counts from the attempt's first start (ruling P2-R8).
        """
        try:
            handle = self._watcher(run, job, spec, row.started_at, Path(row.stdout_path))
        except (KeyError, ValueError) as exc:
            failed = AttemptResult(None, None, None, self.clock.now(), f"bad template: {exc}")
            await self._finish(row, failed, Watch())
            return
        self._handles[run.run_id] = handle
        if run.run_id in self._kill_requested:
            self._signal_kill(run.run_id, handle)
        watch = Watch()
        try:
            result = await self._idle_while(handle)
        finally:
            self._handles.pop(run.run_id, None)
        watch.note(handle)
        await self._finish(row, result, watch)

    async def _adopt(self, run: Run, row: Attempt, pid: int, began: datetime) -> None:
        """Watch a minder that outlived the last scheduler until its attempt ends.

        Its timeout counts from the attempt's real start, and an operator
        kill signals its process group, just as for an attempt started now.
        A kill recorded before the restart is signalled again at once.
        """
        path = Path(row.status_path)
        handle = AdoptedHandle(self.clock, pid, began, path, poll=ADOPT_POLL)
        self._handles[run.run_id] = handle
        if run.run_id in self._kill_requested:
            self._signal_kill(run.run_id, handle)
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
        A job added while the scheduler was down (not in the latest stored
        config record, and with no runs at all) is not caught up either.
        Either way ``last_tick`` becomes ``now``.
        """
        stored = await pynet.loop.run_in_thread(self.store.get_engine, LAST_TICK)
        if stored is not None:
            after = datetime.fromisoformat(stored)
            jobs = await self._catch_up_jobs()
            await self._create_missed(
                [(job.name, firing) for job in jobs for firing in self._missed(job, after, now)]
            )
        await self._flush(engine={LAST_TICK: now.isoformat()})

    async def _catch_up_jobs(self) -> list[JobDef]:
        """Return the enabled, scheduled jobs to catch up, leaving out brand-new ones."""
        jobs = sorted(
            (job for job in self.config.jobs.values() if job.enabled and job.schedules),
            key=lambda job: job.name,
        )
        record = await pynet.loop.run_in_thread(self.store.latest_config)
        known = recorded_jobs(record)
        if known is None:
            return jobs
        store = self.store
        unknown = [job.name for job in jobs if job.name not in known]
        new = await pynet.loop.run_in_thread(
            lambda: {name for name in unknown if not store.query_runs(job=name, limit=1)}
        )
        for name in sorted(new):
            logger.info("job %s is new since the last config: its missed firings are skipped", name)
        return [job for job in jobs if job.name not in new]

    async def _create_missed(self, plan: list[tuple[str, Firing]]) -> None:
        """Create ``catch_up`` runs for ``plan``, after loading the old dates it touches."""
        await self._prepare([(name, firing.run_date) for name, firing in plan])
        for name, firing in plan:
            if name in self.config.jobs:  # a reload may have come meanwhile
                self._create_run(name, firing.run_date, firing.scheduled_at, Trigger.CATCH_UP)

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


def recorded_jobs(record: ConfigRecord | None) -> frozenset[str] | None:
    """Return the job names in a stored config record, or ``None`` if they cannot be told."""
    if record is None:
        return None
    names: set[str] = set()
    try:
        sources: object = json.loads(record.sources_json)
        if not isinstance(sources, dict):
            return None
        for text in cast("dict[str, object]", sources).values():
            root = parse(text) if isinstance(text, str) else None
            jobs = root.items.get("jobs") if isinstance(root, Map) else None
            if isinstance(jobs, Map):
                names.update(jobs.items)
    except ValueError, YamlError:
        return None
    return frozenset(names) or None


def _inspect(store: Store, run: Run) -> _Found:
    """Read a running or retry-waiting run's latest attempt, note, pid and kill (blocking).

    The pid is the attempt row's, or else the ``.pid`` file's. It counts
    as alive only if it is still the minder (``minder_alive``: same user,
    same start time as the ``.pid`` file says, or as the attempt row says
    when there is no file). A ``kill`` event logged after the attempt
    started names the operator who asked for it.
    """
    attempts = store.attempts_for(run.run_id)
    row = next((a for a in attempts if a.attempt == run.attempt), None)
    if run.state is not RunState.RUNNING:
        return _Found(row, None, None)
    since = row.started_at if row is not None else run.updated_at
    kills = [e for e in store.events_for(run.run_id) if e.action == "kill" and e.at >= since]
    killer = kills[-1].actor if kills else None
    if row is None or not row.status_path:  # no row yet, or a watcher (no process)
        return _Found(row, None, None, killer=killer)
    path = Path(row.status_path)
    note = read_pid(path)
    pid = row.pid or (note.pid if note else None)
    began = note.started_at if note and note.pid == pid and note.started_at else row.started_at
    alive = pid is not None and minder_alive(pid, began)
    return _Found(row, read_status(path), pid, alive, killer, began)


async def recover(engine: Recovery) -> None:
    """Load the store and pick every open run up again (see ``Recovery.recover``)."""
    await engine.recover()


async def catch_up(engine: Recovery, now: datetime) -> None:
    """Create the runs of missed firings per policy (see ``Recovery.catch_up``)."""
    await engine.catch_up(now)
