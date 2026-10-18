"""Bring in new work: trigger, backfill and reload.

Back at the train controller's desk, these are the buttons that change
the timetable itself rather than one train. ``trigger`` adds one extra
train now. ``backfill`` adds the trains that never ran on past days.
``reload`` swaps in a new timetable: trains already moving finish their
trip as planned, every other train follows the new timetable at once,
and trains of a line that was closed are cancelled.

As with every action, each change is committed (with its logbook line)
before anything is started.
"""

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.config import Config, JobDef, RunDateRule
from pyscheduler.engine.action_support import ActionError
from pyscheduler.engine.actions import OperatorActions
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.model import Event, Run, RunState, Trigger
from pyscheduler.schedule.firings import firings_between, resolve_local
from pyscheduler.store import ConfigRecord

MAX_BACKFILL_DAYS = 366
"""The longest date range (in calendar days, both ends included) one backfill may cover."""

REMOVED = "removed by reload"
"""The reason on an open run cancelled because a reload removed its job."""

DISABLED = "disabled"
"""The reason on an open run cancelled because a reload disabled its job."""

ONE_DAY = timedelta(days=1)
JUST_BEFORE = timedelta(microseconds=1)
MIDNIGHT = time(0)
_SEARCH_DAYS = 2


def first_firing(job: JobDef, calendar: BusinessCalendar, run_date: date) -> datetime | None:
    """Return when ``job`` first fires for ``run_date``, or ``None`` if it never does.

    A job with no schedule (dependency-only or manual) "fires" at the
    start of the run date in its time zone. For ``previous_business_day``
    jobs the firings for ``run_date`` land on the next business day.
    """
    if not job.schedules:
        return resolve_local(run_date, MIDNIGHT, job.timezone)
    landing = run_date
    if job.run_date_rule is RunDateRule.PREVIOUS_BUSINESS_DAY:
        try:
            landing = calendar.next(run_date)
        except ValueError:
            return None
    begin = resolve_local(landing, MIDNIGHT, job.timezone) - JUST_BEFORE
    end = resolve_local(landing + _SEARCH_DAYS * ONE_DAY, MIDNIGHT, job.timezone)
    for firing in firings_between(job, calendar, begin, end):
        if firing.run_date == run_date:
            return firing.scheduled_at
    return None


class ConfigReload(OperatorActions):
    """Give the engine ``trigger``, ``backfill`` and ``reload``."""

    async def trigger(self, job: str, *, run_date: date | None = None, actor: str) -> Run:
        """Create a manual run of ``job`` now; the run date defaults by the job's rule.

        Without ``run_date`` (see ``default_run_date``): ``same_day`` is
        today in the job's zone, or the latest business day before it when
        today is not one; ``previous_business_day`` is the business day
        before today. Manual runs are always new (the next sequence
        number), and create dependency-only downstream runs like any other run.

        Raises:
            ActionError: 404 for an unknown job, 409 for a disabled one, 503 if stopping.

        """
        definition = self._usable_job(job)
        day = run_date or self.default_run_date(definition)
        await self._load_dates(job, [day])
        await self._seed_old_seqs(job, [day])
        self._usable_job(job)
        made = self._create_runs(job, day, self.clock.now(), Trigger.MANUAL)
        # The downstream runs made along the way log only the scheduler's ``created``.
        return (await self._commit(actor, "trigger", {made[0].run_id: None}))[0]

    def default_run_date(self, job: JobDef) -> date:
        """Return the run date a trigger of ``job`` gets when none is given (ruling R32)."""
        today = self.clock.now().astimezone(job.timezone).date()
        calendar = self.config.calendar_for(job)
        try:
            if job.run_date_rule is RunDateRule.PREVIOUS_BUSINESS_DAY:
                return calendar.previous(today)
            return today if calendar.is_business_day(today) else calendar.previous(today)
        except ValueError:  # a calendar with no business days: nothing better than today
            return today

    async def backfill(self, job: str, *, start: date, end: date, actor: str) -> list[Run]:
        """Create one run per business day from ``start`` to ``end`` on which ``job`` has none.

        Each run is due at the job's first firing for that run date (see
        ``first_firing``); a run date the job never fires for is passed
        over. Dates in the future are allowed: their runs start now, and
        the later real firing finds its run already there. Backfilled runs
        create dependency-only downstream runs like any other run.

        Raises:
            ActionError: 400 for a reversed or too long range, 404 for an
                unknown job, 409 for a disabled one, 503 if stopping.

        """
        definition = self._usable_job(job)
        if start > end:
            raise ActionError(400, f"backfill start {start} is after end {end}")
        if (end - start).days + 1 > MAX_BACKFILL_DAYS:
            raise ActionError(400, f"backfill covers more than {MAX_BACKFILL_DAYS} days")
        calendar = self.config.calendar_for(definition)
        span = range((end - start).days + 1)
        days = [d for n in span if calendar.is_business_day(d := start + n * ONE_DAY)]
        await self._load_dates(job, days)
        await self._seed_old_seqs(job, days)
        definition = self._usable_job(job)
        calendar = self.config.calendar_for(definition)
        filled: dict[str, RunState | None] = {}
        for day in days:
            due = first_firing(definition, calendar, day)
            if self.book.runs_for(job, day) or due is None:
                continue
            made = self._create_runs(job, day, due, Trigger.BACKFILL)
            if made:
                filled[made[0].run_id] = None
        return await self._commit(actor, "backfill", filled)

    async def reload(self, new_config: Config, *, actor: str) -> None:
        """Swap in ``new_config`` (already validated) and apply the reload rules.

        - Runs with a running attempt keep the definition they started
          with; when the attempt ends, a removed or disabled job does not
          retry.
        - Every other open run adopts the new definition (and version) and
          is re-evaluated at once. A queued run goes back to ``scheduled``
          to be placed again (dependencies and overlap of the new
          definition), except a force-started one, which is re-queued with
          the new criticality and pools.
        - Open, not running runs of a removed or disabled job are cancelled.
        - The new limits apply at once, and the firing heap is rebuilt
          from now, in the same step as the config swap, so no firing of
          the old heap fires under the new config (firings between the old
          and the new config are not caught up).

        A ``ConfigRecord`` with the next version and one ``reload`` event
        (no run id) are committed with the changes. A config with the
        same digest as the current one changes nothing. Once the commit is
        durable the reload listeners are told (``watch_reloads``).

        Raises:
            ActionError: 503 if the engine is stopping or the commit failed.

        """
        self._check_usable()
        if new_config.digest == self.config.digest:
            return
        before = self.config_version
        version = before + 1
        now = self.clock.now()
        self.config = new_config
        self.config_version = version
        self.admission.set_limits(new_config.settings.max_running, new_config.pools)
        self._restart_firings()
        cancelled: list[str] = []
        for run in self.book.open_runs():
            if run.state is RunState.RUNNING:
                continue
            job = new_config.jobs.get(run.job)
            if job is None or not job.enabled:
                self._unqueue(run.run_id)
                reason = REMOVED if job is None else DISABLED
                self._stage(replace(self._changed(run, RunState.CANCELLED, reason), held_from=None))
                cancelled.append(run.run_id)
                continue
            adopted = replace(run, config_version=version, updated_at=now)
            if run.run_id in self._force and run.run_id in self.ready:
                self._enqueue(adopted, job)
            elif run.state is RunState.QUEUED:
                self._unqueue(run.run_id)
                adopted = replace(adopted, state=RunState.SCHEDULED, reason="")
            self._stage(adopted)
        self._reevaluate(sorted(new_config.jobs))
        record = ConfigRecord(
            version, now, new_config.digest, json.dumps(dict(new_config.sources), sort_keys=True)
        )
        detail: dict[str, object] = {
            "before": before,
            "after": version,
            "digest": new_config.digest,
            "cancelled": cancelled,
        }
        self._staged_events.append(Event(now, actor, None, "reload", json.dumps(detail)))
        try:
            await self._flush(configs=[record])
        except EngineFailedError as exc:
            raise ActionError(503, f"scheduler failed: {exc}") from exc
        self._notify_reloaded()
