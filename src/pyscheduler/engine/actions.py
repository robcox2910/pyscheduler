"""Operator actions: the buttons a person can press on a run.

Think of a train controller's desk. Trains (runs) mostly follow the
timetable on their own, but the controller can add an extra train
(``trigger``), send one round again (``rerun``), hold one at the
platform (``hold`` / ``release``), stop one (``kill``), wave one
through a red signal (``force_start``), or cross one off the board
(``skip``, ``mark_success``, ``mark_failed``). Every press is written
in the logbook with the controller's name *before* anything moves.

Each action follows one recipe: check the engine is working, check the
run may make this move, stage every change with its event, commit them
all in one flush (all or nothing), and only then act (start, signal).
"""

import json
from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime

import pynet.loop

from pyscheduler.config import JobDef
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.creation import RunCreation, downstream_date
from pyscheduler.engine.model import Event, Run, RunState

LOAD_LIMIT = 1_000_000
"""The most stored runs read back for one run date (far more than any date holds)."""

_HOLDABLE = frozenset(
    {RunState.SCHEDULED, RunState.WAITING, RunState.BLOCKED, RunState.QUEUED, RunState.RETRY_WAIT}
)
_FORCEABLE = frozenset(
    {
        RunState.SCHEDULED,
        RunState.WAITING,
        RunState.BLOCKED,
        RunState.QUEUED,
        RunState.HELD,
        RunState.RETRY_WAIT,
    }
)
_REEVALUATED = frozenset({RunState.SCHEDULED, RunState.WAITING, RunState.BLOCKED})


class ActionError(Exception):
    """Raised when an operator action cannot be done; ``status`` is the HTTP-style code.

    Attributes:
        status: 400 bad request, 404 unknown job or run, 409 not allowed
            in the run's (or job's) current state, 503 the engine is
            stopping or has failed stop.

    """

    def __init__(self, status: int, message: str) -> None:
        """Remember the status code and the message."""
        super().__init__(message)
        self.status = status


class OperatorActions(RunCreation):
    """Give the engine its operator actions (see the module docstring)."""

    # -- rerun -----------------------------------------------------------

    async def rerun(self, run_id: str, *, downstream: bool = False, actor: str) -> list[Run]:
        """Reopen a final run (and, with ``downstream``, its final downstream runs).

        Each reopened run gets ``rerun = attempt`` (a fresh retry budget),
        goes back to ``scheduled`` and is re-evaluated at once. Downstream
        runs are found through ``dependents``: for a dependency with a
        run-date offset, the downstream run date is the one whose offset
        points at the upstream's run date (as for dependency-only runs).
        Only the latest run of each downstream ``(job, run date)`` is
        reopened, and only if it is final; the search goes on through the
        runs it reopens. Downstream jobs that are removed or disabled are
        left alone.

        Raises:
            ActionError: 404 unknown run, 409 run not final or job gone or
                disabled, 503 if stopping.

        """
        run = await self._find(run_id)
        if not run.is_final:
            raise ActionError(409, f"run {run_id} is {run.state.value}, not final")
        self._usable_job(run.job, status=409)
        now = self.clock.now()
        befores: dict[str, RunState | None] = {}
        jobs: dict[str, None] = {}
        todo = deque([run])
        while todo:
            current = todo.popleft()
            befores[current.run_id] = current.state
            jobs[current.job] = None
            reopened = replace(
                current,
                state=RunState.SCHEDULED,
                reason=f"rerun by {actor}",
                rerun=current.attempt,
                held_from=None,
                updated_at=now,
            )
            self._stage(reopened)
            if downstream:
                todo.extend(r for r in self._final_downstream(reopened) if r.run_id not in befores)
        self._reevaluate(jobs)
        return await self._commit(actor, "rerun", befores)

    def _final_downstream(self, upstream: Run) -> list[Run]:
        """Return the final latest runs of ``upstream``'s dependents for the matching dates."""
        found: list[Run] = []
        for name in sorted(self.config.dependents(upstream.job)):
            job = self.config.jobs.get(name)
            if job is None or not job.enabled:
                continue
            calendar = self.config.calendar_for(job)
            for dep in job.depends_on:
                if dep.job != upstream.job:
                    continue
                day = downstream_date(upstream.run_date, dep.run_date_offset, calendar)
                latest = self.book.latest_for(name, day) if day is not None else None
                if latest is not None and latest.is_final and latest not in found:
                    found.append(latest)
        return found

    # -- hold and release ------------------------------------------------

    async def hold(self, run_id: str, *, actor: str) -> Run:
        """Freeze a run that is scheduled, waiting, blocked, queued or retry-waiting.

        The run leaves the ready queue and its retry timer is cancelled;
        ``held_from`` remembers where it was. Its job's other runs are
        re-checked, since a held run no longer counts for ``overlap``.

        Raises:
            ActionError: 404 unknown run, 409 not holdable, 503 if stopping.

        """
        run = await self._find(run_id)
        if run.state not in _HOLDABLE:
            raise ActionError(409, f"cannot hold run {run_id}: it is {run.state.value}")
        self._unqueue(run_id)
        held = replace(self._changed(run, RunState.HELD, f"held by {actor}"), held_from=run.state)
        self._stage(held)
        self._reevaluate([run.job])
        return (await self._commit(actor, "hold", {run_id: run.state}))[0]

    async def release(self, run_id: str, *, actor: str) -> Run:
        """Return a held run to ``held_from`` and re-check it at once.

        A run held while scheduled, waiting or blocked is re-evaluated
        (dependencies and overlap). One held while queued goes back
        through the overlap rule into the queue. One held while
        retry-waiting is queued now: the hold counts as its wait.

        Raises:
            ActionError: 404 unknown run, 409 not held (or its job is gone), 503 if stopping.

        """
        run = await self._find(run_id)
        if run.state is not RunState.HELD or run.held_from is None:
            raise ActionError(409, f"cannot release run {run_id}: it is {run.state.value}")
        job = self._usable_job(run.job, status=409)
        back = replace(self._changed(run, run.held_from, f"released by {actor}"), held_from=None)
        self._stage(back)
        if back.state in _REEVALUATED:
            self._reevaluate([run.job])
        else:
            self._reevaluate([run.job, *self._ready(back, job)])
        return (await self._commit(actor, "release", {run_id: run.state}))[0]

    # -- kill and force-start --------------------------------------------

    async def kill(self, run_id: str, *, actor: str) -> Run:
        """Stop a running attempt: SIGTERM now, SIGKILL after ``kill_grace``; no retry.

        The kill is recorded first, then signalled. An attempt still being
        launched (no handle yet) is signalled as soon as it has one. The
        run ends ``failed`` with reason ``killed by <actor>``.

        Raises:
            ActionError: 404 unknown run, 409 not running, 503 if stopping.

        """
        run = await self._find(run_id)
        if run.state is not RunState.RUNNING:
            raise ActionError(409, f"cannot kill run {run_id}: it is {run.state.value}")
        if run_id in self._kill_requested:
            raise ActionError(409, f"run {run_id} is already being killed")
        result = (await self._commit(actor, "kill", {run_id: run.state}))[0]
        current = self.book.get(run_id)
        same_attempt = current is not None and current.attempt == run.attempt
        if current is not None and current.state is RunState.RUNNING and same_attempt:
            self._kill_requested.add(run_id)
            self._kill_actors[run_id] = actor
            handle = self._handles.get(run_id)
            if handle is not None:
                self._signal_kill(run_id, handle)
        return result

    async def force_start(self, run_id: str, *, actor: str) -> Run:
        """Queue a run now, first in line, ignoring its dependencies and ``overlap``.

        Pools and the global cap still apply. Allowed from scheduled,
        waiting, blocked, held, retry_wait, and queued (to jump the line).

        Raises:
            ActionError: 404 unknown run, 409 not allowed (or its job is gone), 503 if stopping.

        """
        run = await self._find(run_id)
        if run.state not in _FORCEABLE:
            raise ActionError(409, f"cannot force-start run {run_id}: it is {run.state.value}")
        job = self._usable_job(run.job, status=409)
        self._unqueue(run_id)
        queued = replace(
            self._changed(run, RunState.QUEUED, f"force-started by {actor}"), held_from=None
        )
        self._stage(queued)
        self._needs[run_id] = dict(job.pools)
        self.ready.push(queued, job.criticality, job.pools)
        self._force.add(run_id)
        return (await self._commit(actor, "force_start", {run_id: run.state}))[0]

    # -- ending by hand --------------------------------------------------

    async def skip(self, run_id: str, *, actor: str) -> Run:
        """End an open, not running run as skipped ("pretend it worked")."""
        return await self._end(run_id, RunState.SKIPPED, "skip", actor, final_ok=False)

    async def mark_success(self, run_id: str, *, actor: str) -> Run:
        """Set a run that is not running to succeeded (final runs included)."""
        return await self._end(run_id, RunState.SUCCEEDED, "mark_success", actor, final_ok=True)

    async def mark_failed(self, run_id: str, *, actor: str) -> Run:
        """Set a run that is not running to failed (final runs included)."""
        return await self._end(run_id, RunState.FAILED, "mark_failed", actor, final_ok=True)

    async def _end(
        self, run_id: str, state: RunState, action: str, actor: str, *, final_ok: bool
    ) -> Run:
        """Set ``state`` by hand, drop the run's queue entry and timer, re-check dependents.

        Raises:
            ActionError: 404 unknown run, 409 running (or final, unless
                ``final_ok``), 503 if stopping.

        """
        run = await self._find(run_id)
        if run.state is RunState.RUNNING or (run.is_final and not final_ok):
            raise ActionError(409, f"cannot {action} run {run_id}: it is {run.state.value}")
        self._unqueue(run_id)
        ended = replace(self._changed(run, state, f"{action} by {actor}"), held_from=None)
        self._stage(ended)
        self._reevaluate([run.job])
        return (await self._commit(actor, action, {run_id: run.state}))[0]

    # -- shared steps ----------------------------------------------------

    def _check_usable(self) -> None:
        """Refuse to act once the engine has failed stop or is stopping.

        Raises:
            ActionError: 503.

        """
        if self.error is not None:
            raise ActionError(503, f"scheduler failed: {self.error}")
        if self._stopping:
            raise ActionError(503, "scheduler is stopping")

    def _usable_job(self, name: str, *, status: int = 404) -> JobDef:
        """Return the enabled job ``name`` (after checking the engine is usable).

        Raises:
            ActionError: ``status`` if the job is unknown, 409 if it is
                disabled, 503 if the engine is stopping.

        """
        self._check_usable()
        job = self.config.jobs.get(name)
        if job is None:
            raise ActionError(status, f"unknown job {name!r}")
        if not job.enabled:
            raise ActionError(409, f"job {name!r} is disabled")
        return job

    async def _find(self, run_id: str) -> Run:
        """Return the run ``run_id`` from memory, or loaded back from the store.

        A final run that memory has let go of is read back, together with
        the runs its dependencies and dependents look at.

        Raises:
            ActionError: 404 if there is no such run, 503 if stopping.

        """
        self._check_usable()
        run = self.book.get(run_id)
        if run is None:
            stored = await pynet.loop.run_in_thread(self.store.get_run, run_id)
            if stored is not None:
                await self._load_dates(stored.job, [stored.run_date])
            self._check_usable()
            run = self.book.get(run_id)
        if run is None:
            raise ActionError(404, f"unknown run {run_id!r}")
        return run

    async def _load_dates(self, job: str, days: Iterable[date]) -> None:
        """Read back the stored final runs that memory let go of, for the dates ``job`` touches.

        Those are ``days``, the dates of every downstream job (transitively)
        and the dates their dependencies look at. Only final runs missing
        from memory are added: every open run is always in memory.
        """
        wanted: set[date] = set()
        todo = deque((job, day) for day in days)
        seen: set[tuple[str, date]] = set()
        while todo:
            name, day = todo.popleft()
            if (name, day) in seen:
                continue
            seen.add((name, day))
            wanted.add(day)
            definition = self.config.jobs.get(name)
            if definition is not None:
                wanted.update(self._upstream_dates(definition, day))
            for down in sorted(self.config.dependents(name)):
                child = self.config.jobs.get(down)
                if child is None:
                    continue
                calendar = self.config.calendar_for(child)
                for dep in child.depends_on:
                    mapped = downstream_date(day, dep.run_date_offset, calendar)
                    if dep.job == name and mapped is not None:
                        todo.append((down, mapped))
        for day in sorted(wanted):
            stored = await pynet.loop.run_in_thread(
                self.store.query_runs, run_date=day, limit=LOAD_LIMIT
            )
            for run in stored:
                if run.is_final and self.book.get(run.run_id) is None:
                    self.book.put(run)
                    self.book.publish(run)

    def _upstream_dates(self, job: JobDef, day: date) -> set[date]:
        """Return the run dates ``job``'s dependencies look at for ``day``."""
        calendar = self.config.calendar_for(job)
        dates: set[date] = set()
        for dep in job.depends_on:
            try:
                dates.add(calendar.shift(day, dep.run_date_offset))
            except ValueError:
                continue
        return dates

    async def _commit(
        self, actor: str, action: str, befores: dict[str, RunState | None], **extra: object
    ) -> list[Run]:
        """Add one event per run in ``befores``, flush, and return the committed runs.

        Raises:
            ActionError: 503 if the commit failed (the engine has failed stop).

        """
        now: datetime = self.clock.now()
        for run_id, before in befores.items():
            run = self.book.get(run_id)
            after = run.state.value if run is not None else None
            detail = {
                "before": before.value if before is not None else None,
                "after": after,
                **extra,
            }
            self._staged_events.append(Event(now, actor, run_id, action, json.dumps(detail)))
        try:
            await self._flush()
        except EngineFailedError as exc:
            raise ActionError(503, f"scheduler failed: {exc}") from exc
        committed: list[Run] = []
        for run_id in befores:
            run = self.book.published(run_id) or self.book.get(run_id)
            if run is not None:
                committed.append(run)
        return committed
