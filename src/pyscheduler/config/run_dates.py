"""Run dates: which homework days each job will really get a run for.

Picture a class register for the coming year. For every job it ticks the
days that job will have a run for: a scheduled job from its alarm clock,
a dependency-only job from the jobs it follows. Validation reads the
register to spot a job that would wait for a tick that never comes.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.config.model import Config, JobDef
from pyscheduler.schedule.firings import firings_between, firings_on

__all__ = [
    "MAX_CHECKED_OFFSET",
    "PAD_DAYS",
    "REPEAT_HORIZON_DAYS",
    "WAIT_HORIZON_DAYS",
    "RunDateIndex",
    "RunDates",
]

WAIT_HORIZON_DAYS = 366
REPEAT_HORIZON_DAYS = 31
PAD_DAYS = 31
"""Extra landing days scanned either side, so shifted run dates are still seen."""
MAX_CHECKED_OFFSET = 260
"""Offsets larger than about a year of business days are not followed."""

type RunDates = frozenset[date] | None
"""A job's run dates in the scanned window, or ``None`` when they cannot be known."""


class RunDateIndex:
    """Work out, and remember, each job's firings and run dates around a year ahead."""

    def __init__(
        self,
        config: Config,
        today: date,
        calendar: Callable[[JobDef], BusinessCalendar | None],
    ) -> None:
        """Index ``config`` from ``today``; ``calendar`` gives a job's usable calendar."""
        self.config = config
        self.today = today
        self.start = datetime.combine(today, time(0), tzinfo=UTC)
        self.calendar = calendar
        self._landings: dict[str, tuple[tuple[date, date], ...] | None] = {}
        self._run_dates: dict[str, RunDates] = {}
        self._repeats: dict[str, bool] = {}

    def repeats(self, job: JobDef) -> bool:
        """Return whether ``job`` fires twice for one run date in the next 31 days."""
        if job.name not in self._repeats:
            self._repeats[job.name] = self._find_repeat(job)
        return self._repeats[job.name]

    def _find_repeat(self, job: JobDef) -> bool:
        """Scan the canonical firing stream for a repeated run date."""
        cal = self.calendar(job)
        if not job.schedules or cal is None:
            return False
        seen: set[date] = set()
        end = self.start + timedelta(days=REPEAT_HORIZON_DAYS)
        try:
            for firing in firings_between(job, cal, self.start, end):
                if firing.run_date in seen:
                    return True
                seen.add(firing.run_date)
        except ValueError:
            return False
        return False

    def landings(self, job: JobDef) -> tuple[tuple[date, date], ...] | None:
        """Return ``(landing day, run date)`` pairs around the next 366 days.

        Landing days run from ``PAD_DAYS`` before today to ``PAD_DAYS``
        after the horizon. ``None`` means they cannot be worked out.
        """
        if job.name in self._landings:
            return self._landings[job.name]
        cal = self.calendar(job)
        pairs: tuple[tuple[date, date], ...] | None = None
        if cal is not None:
            first = self.today - timedelta(days=PAD_DAYS)
            days = (first + timedelta(days=i) for i in range(WAIT_HORIZON_DAYS + 2 * PAD_DAYS))
            try:
                pairs = tuple(
                    (day, run_date)
                    for day in days
                    for run_date in sorted({f.run_date for f in firings_on(job, day, cal)})
                )
            except ValueError:
                pairs = None
        self._landings[job.name] = pairs
        return pairs

    def run_dates(self, job: JobDef) -> RunDates:
        """Return the run dates ``job`` gets runs for, or ``None`` if unknowable.

        A scheduled job's come from its firings. A dependency-only job gets
        a run whenever an upstream does, with the offset applied in reverse.
        A disabled job gets none; a manual-only job is unknowable.
        """
        if job.name in self._run_dates:
            return self._run_dates[job.name]
        self._run_dates[job.name] = None  # guards against offset cycles
        result: RunDates = None
        if not job.enabled:
            result = frozenset()
        elif job.schedules:
            pairs = self.landings(job)
            result = None if pairs is None else frozenset(r for _, r in pairs)
        elif job.dependency_only:
            result = self._derived_run_dates(job)
        self._run_dates[job.name] = result
        return result

    def _derived_run_dates(self, job: JobDef) -> RunDates:
        """Return a dependency-only job's run dates, from its upstreams'."""
        cal = self.calendar(job)
        if cal is None:
            return None
        dates: set[date] = set()
        for dep in job.depends_on:
            up = self.config.jobs.get(dep.job)
            if up is None or not -MAX_CHECKED_OFFSET <= dep.run_date_offset <= 0:
                return None
            theirs = self.run_dates(up)
            if theirs is None:
                return None
            try:
                dates.update(cal.shift(d, -dep.run_date_offset) for d in theirs)
            except ValueError:
                return None
        return frozenset(dates)
