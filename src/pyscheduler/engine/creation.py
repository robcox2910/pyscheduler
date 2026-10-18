"""Create runs and move them between waiting, blocked, skipped and queued.

Think of a cake shop's order board. A new order goes up as *scheduled*.
If the sponge is not baked yet, the icing order *waits*; if the oven
broke, it is *blocked*; once everything it needs is ready it joins the
*queue* for a free baker. Every time an order is finished, the shop
looks again, but only at the orders that were waiting on that one.

``RunCreation`` does that bookkeeping. It only *stages* its changes
(see ``EngineBase._stage``); the next flush makes them durable before
anything is started.
"""

from collections import deque
from collections.abc import Iterable
from datetime import date, datetime

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.config import JobDef, Overlap
from pyscheduler.engine.base import EngineBase
from pyscheduler.engine.deps import Verdict, evaluate
from pyscheduler.engine.model import Run, RunState, Trigger, make_run_id
from pyscheduler.engine.runbook import OVERLAP_REASON

SUPERSEDED = "superseded"
"""The reason on an overlap-waiting run replaced by a newer one of the same run date."""

SKIPPED_OVERLAP = "skipped: overlap"
"""The reason on a run cancelled because its job was busy and ``overlap`` is ``skip``."""

_SCHEDULED_TRIGGERS = frozenset({Trigger.SCHEDULE, Trigger.CATCH_UP, Trigger.BACKFILL})
_EVALUABLE = frozenset({RunState.SCHEDULED, RunState.WAITING, RunState.BLOCKED})
_VERDICT_STATES = {
    Verdict.WAIT: RunState.WAITING,
    Verdict.BLOCK: RunState.BLOCKED,
    Verdict.SKIP: RunState.SKIPPED,
}


def downstream_date(upstream_date: date, offset: int, calendar: BusinessCalendar) -> date | None:
    """Return the run date ``R`` whose dependency with ``offset`` points at ``upstream_date``.

    ``R`` is the date with ``calendar.shift(R, offset) == upstream_date``,
    or ``None`` if there is none (for example, the upstream date is not a
    business day of ``calendar``).
    """
    if not offset:
        return upstream_date
    try:
        run_date = calendar.shift(upstream_date, -offset)
        back = calendar.shift(run_date, offset)
    except ValueError:
        return None
    return run_date if back == upstream_date else None


def _order(run: Run) -> tuple[date, datetime, str]:
    """Return the order runs of one job are looked at in: run date, due time, id."""
    return (run.run_date, run.scheduled_at, run.run_id)


class RunCreation(EngineBase):
    """Create runs and re-check their dependencies and overlap."""

    def _create_run(
        self, job: str, run_date: date, scheduled_at: datetime, trigger: Trigger
    ) -> Run | None:
        """Create a run of ``job`` (and its dependency-only downstream runs), staged.

        A schedule, catch-up or backfill run is unique by ``(job,
        scheduled_at)``; a dependency run is made only if the job has no
        dependency or backfill run for ``run_date``. A duplicate returns
        ``None``. Manual runs are always new. The new runs are evaluated
        at once, so the returned copy may already be queued, waiting or
        even cancelled by the overlap rule.
        """
        created = self._create_runs(job, run_date, scheduled_at, trigger)
        return self.book.get(created[0].run_id) if created else None

    def _create_runs(
        self, job: str, run_date: date, scheduled_at: datetime, trigger: Trigger
    ) -> list[Run]:
        """Create a run as ``_create_run`` does; return every run made, the first first.

        The dependency-only chain is followed at most as many steps as
        there are jobs. A longer chain can only come from a cycle through
        run-date offsets (``b`` needs yesterday's ``a``, ``a`` needs
        today's ``b``), which would otherwise make runs for ever.
        """
        first = self._new_run(job, run_date, scheduled_at, trigger)
        if first is None:
            return []
        created = [first]
        todo = deque([(first, 0)])
        while todo:
            upstream, depth = todo.popleft()
            if depth >= len(self.config.jobs):
                continue
            for downstream in self._dependency_runs(upstream):
                created.append(downstream)
                todo.append((downstream, depth + 1))
        self._reevaluate(dict.fromkeys(run.job for run in created))
        return created

    def _new_run(
        self, job: str, run_date: date, scheduled_at: datetime, trigger: Trigger
    ) -> Run | None:
        """Stage a new ``scheduled`` run unless one already exists for that slot."""
        if trigger in _SCHEDULED_TRIGGERS and self.book.by_scheduled(job, scheduled_at):
            return None
        if trigger is Trigger.DEPENDENCY and (
            self.book.dependency_run(job, run_date)
            or any(r.trigger is Trigger.BACKFILL for r in self.book.runs_for(job, run_date))
        ):
            return None  # one per run date; a backfill counts, a manual run does not
        now = self.clock.now()
        run = Run(
            run_id=make_run_id(job, run_date, self.book.next_seq(job, run_date)),
            job=job,
            run_date=run_date,
            scheduled_at=scheduled_at,
            state=RunState.SCHEDULED,
            reason="",
            attempt=0,
            rerun=0,
            trigger=trigger,
            config_version=self.config_version,
            created_at=now,
            updated_at=now,
        )
        created = self._event(
            run.run_id, "created", trigger=trigger.value, scheduled_at=scheduled_at.isoformat()
        )
        self._stage(run, created)
        return run

    def _dependency_runs(self, upstream: Run) -> list[Run]:
        """Create the runs of dependency-only jobs that ``upstream``'s run date calls for.

        For a dependency with offset ``k``, the downstream run date ``R``
        is the one with ``shift(R, k) == upstream.run_date`` on the
        downstream job's calendar. A job's dependency on its own earlier
        run dates never creates runs (each run would make the next, for ever).
        """
        made: list[Run] = []
        for name in sorted(self.config.dependents(upstream.job)):
            down = self.config.jobs.get(name)
            if down is None or not down.dependency_only or not down.enabled:
                continue
            if name == upstream.job:
                continue
            calendar = self.config.calendar_for(down)
            for dep in down.depends_on:
                if dep.job != upstream.job:
                    continue
                run_date = downstream_date(upstream.run_date, dep.run_date_offset, calendar)
                if run_date is None:
                    continue
                run = self._new_run(name, run_date, upstream.scheduled_at, Trigger.DEPENDENCY)
                if run is not None:
                    made.append(run)
        return made

    def _reevaluate(self, job_names: Iterable[str]) -> None:
        """Re-check the open runs of ``job_names`` and of their dependents.

        Runs that are scheduled, waiting or blocked move to wherever their
        dependencies and the overlap rule now put them. A run that ends up
        final (skipped or cancelled) has its own job's dependents checked
        in turn. Held, queued, running and retry-waiting runs are left alone.
        """
        todo = deque(job_names)
        while todo:
            name = todo.popleft()
            for target in (name, *sorted(self.config.dependents(name))):
                for run in sorted(self.book.open_for(target), key=_order):
                    current = self.book.get(run.run_id)
                    if current is None or current.state not in _EVALUABLE:
                        continue
                    todo.extend(self._evaluate(current))

    def _evaluate(self, run: Run) -> list[str]:
        """Move ``run`` per its dependencies; return the jobs of runs that became final."""
        job = self.config.jobs.get(run.job)
        if job is None:
            return []
        result = evaluate(job, run.run_date, self.config.calendar_for(job), self.book)
        if result.verdict is Verdict.READY:
            return self._ready(run, job)
        state = _VERDICT_STATES[result.verdict]
        if state is run.state and result.reason == run.reason:
            return []
        changed = self._changed(run, state, result.reason)
        self._stage(changed)
        return [run.job] if changed.is_final else []

    def _ready(self, run: Run, job: JobDef) -> list[str]:
        """Queue ``run`` unless the overlap rule holds it back or cancels it.

        A run that already had attempts (a retry, or a rerun) is part of an
        attempt chain: it only ever waits for the active run, and is never
        skipped, superseded, or the cause of superseding. The waiting
        reason is the plain ``OVERLAP_REASON``, so a run already waiting is
        not written again. Return the jobs of runs that became final.
        """
        active = [other for other in self.book.active_for(job.name) if other.run_id != run.run_id]
        if not active or job.overlap is Overlap.ALLOW:
            self._queue(run, job)
            return []
        fresh = run.attempt == 0
        if job.overlap is Overlap.SKIP and fresh:
            self._stage(self._changed(run, RunState.CANCELLED, SKIPPED_OVERLAP))
            return [job.name]
        finals: list[str] = []
        if fresh:
            mine = (run.scheduled_at, run.run_id)
            for waiting in self.book.overlap_waiters(job.name, run.run_date):
                if waiting.run_id == run.run_id or waiting.attempt:
                    continue
                if (waiting.scheduled_at, waiting.run_id) > mine:
                    self._stage(self._changed(run, RunState.CANCELLED, SUPERSEDED))
                    return [job.name]
                self._stage(self._changed(waiting, RunState.CANCELLED, SUPERSEDED))
                finals.append(job.name)
        if run.state is not RunState.WAITING or run.reason != OVERLAP_REASON:
            self._stage(self._changed(run, RunState.WAITING, OVERLAP_REASON))
        return finals
