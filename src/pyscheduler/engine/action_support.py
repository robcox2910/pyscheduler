"""The steps every operator action shares: check, find, commit.

Before a train controller presses any button, the same checks happen:
is the control room working, is that train on the board (or in the
archive), and once the press is written in the logbook, is the page
safely filed? ``ActionSupport`` holds those shared steps; the buttons
themselves live in ``actions`` and ``reload``.
"""

import json
from collections import deque
from collections.abc import Iterable
from datetime import date, datetime, timedelta

import pynet.loop

from pyscheduler.config import JobDef
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.creation import RunCreation, downstream_date
from pyscheduler.engine.model import Event, Run, RunState

LOAD_LIMIT = 1_000_000
"""The most stored runs read back for one run date (far more than any date holds)."""


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


class ActionSupport(RunCreation):
    """Provide the checks, store look-ups and commit shared by every action."""

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
                await self._load_dates(stored.job, [stored.run_date], force=True)
            self._check_usable()
            run = self.book.get(run_id)
        if run is None:
            raise ActionError(404, f"unknown run {run_id!r}")
        return run

    async def _load_dates(self, job: str, days: Iterable[date], *, force: bool = False) -> None:
        """Read back the stored final runs memory let go of, for the dates ``job`` touches.

        Those are ``days``, the dates of ``job``'s downstream jobs
        (transitively) and the dates their dependencies look at. Only
        dates older than the in-memory window (``memory_days``) and within
        ``retention`` can hold such runs, and the walk stops at a date with
        no stored runs, so it always ends (even for a job that depends on
        its own previous run date). ``force`` loads ``days`` themselves
        whatever their age (the caller knows a stored run is there). Only
        final runs missing from memory are added: open runs are always in
        memory.
        """
        settings = self.config.settings
        today = self.clock.now().astimezone(settings.timezone).date()
        newest = self._memory_start()
        oldest = today - settings.retention
        known: dict[date, bool] = {}
        todo = deque((job, day, force) for day in days)
        seen: set[tuple[str, date]] = set()
        while todo:
            name, day, forced = todo.popleft()
            if (name, day) in seen or not (forced or oldest <= day < newest):
                continue
            seen.add((name, day))
            if not await self._load_date(day, known):
                continue  # nothing stored that day, so nothing downstream either
            definition = self.config.jobs.get(name)
            upstream = self._upstream_dates(definition, day) if definition else set[date]()
            for up in sorted(upstream):
                if oldest <= up < newest:
                    await self._load_date(up, known)
            for down in sorted(self.config.dependents(name)):
                child = self.config.jobs.get(down)
                if child is None:
                    continue
                calendar = self.config.calendar_for(child)
                for dep in child.depends_on:
                    mapped = downstream_date(day, dep.run_date_offset, calendar)
                    if dep.job == name and mapped is not None:
                        todo.append((down, mapped, False))

    def memory_start(self) -> date:
        """Return the oldest run date whose runs are all in memory (``memory_days``).

        Runs dated before it may sit only in the store; readers such as
        the API ask the store for those.
        """
        return self._memory_start()

    def _memory_start(self, now: datetime | None = None) -> date:
        """Return the oldest run date whose final runs stay in memory (``memory_days``)."""
        settings = self.config.settings
        today = (now or self.clock.now()).astimezone(settings.timezone).date()
        return today - timedelta(days=max(settings.memory_days - 1, 0))

    async def _seed_old_seqs(self, job: str, days: Iterable[date]) -> None:
        """Seed the sequence numbers of the old dates new runs of ``job`` may land on.

        Final runs older than the in-memory window may sit in the store
        unloaded, so before runs are made for ``(job, day)`` (and for the
        dependency-only runs that follow), the store is asked for the
        highest sequence used, and a new run never reuses a run id.
        """
        newest = self._memory_start()
        old = sorted(slot for slot in self._creation_slots(job, days) if slot[1] < newest)
        if not old:
            return
        store = self.store
        found = await pynet.loop.run_in_thread(lambda: [store.max_seq(*slot) for slot in old])
        for (name, day), seq in zip(old, found, strict=True):
            self.book.seed_seq(name, day, seq)

    def _creation_slots(self, job: str, days: Iterable[date]) -> set[tuple[str, date]]:
        """Return the ``(job, run date)`` slots a run of ``job`` on ``days`` may create runs in.

        Those are the days themselves and, through dependency-only
        downstream jobs, the run dates their dependencies map to (followed
        at most as many steps as there are jobs, like ``_create_runs``).
        """
        slots: set[tuple[str, date]] = set()
        todo = deque((job, day, 0) for day in days)
        while todo:
            name, day, depth = todo.popleft()
            if (name, day) in slots:
                continue
            slots.add((name, day))
            if depth >= len(self.config.jobs):
                continue
            for down in sorted(self.config.dependents(name)):
                child = self.config.jobs.get(down)
                if child is None or not child.dependency_only or down == name:
                    continue
                calendar = self.config.calendar_for(child)
                for dep in child.depends_on:
                    mapped = downstream_date(day, dep.run_date_offset, calendar)
                    if dep.job == name and mapped is not None:
                        todo.append((down, mapped, depth + 1))
        return slots

    async def _load_date(self, day: date, known: dict[date, bool]) -> bool:
        """Load ``day``'s stored final runs missing from memory; return whether any are stored."""
        if day not in known:
            stored = await pynet.loop.run_in_thread(
                self.store.query_runs, run_date=day, limit=LOAD_LIMIT
            )
            known[day] = bool(stored)
            for run in stored:
                if run.is_final and self.book.get(run.run_id) is None:
                    self.book.put(run)
                    self.book.publish(run)
        return known[day]

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
        self,
        actor: str,
        action: str,
        befores: dict[str, RunState | None],
        *,
        created: Iterable[str] = (),
    ) -> list[Run]:
        """Add one event per run in ``befores`` and ``created``, flush, return the committed runs.

        ``created`` are runs made along the way (dependency-only downstream
        runs): they get an event (``before`` is ``None``) but are not
        returned.

        Raises:
            ActionError: 503 if the commit failed (the engine has failed stop).

        """
        now = self.clock.now()
        logged = {**befores, **{run_id: None for run_id in created if run_id not in befores}}
        for run_id, before in logged.items():
            run = self.book.get(run_id)
            detail = {
                "before": before.value if before is not None else None,
                "after": run.state.value if run is not None else None,
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
