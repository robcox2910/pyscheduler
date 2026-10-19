"""Forecast: the weather report for jobs.

A forecast says "around this time, this job will probably start". It walks
the schedules for the window you ask about, then follows the dependencies:
a follower is expected to start when its slowest upstream is expected to
finish, using the *typical* (median) duration from past runs.

The forecast assumes the happy path: every upstream succeeds, so the ``on:``
kind of a dependency is ignored and failure-only jobs still show up. Loops
through earlier run dates (a job needing its own yesterday) are fine. A
dependency-only job appears for every run date that has an upstream entry in
the window, even if its expected start lands after ``end``.
"""

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pyscheduler.config import Config, JobDef
from pyscheduler.schedule.firings import firings_between

__all__ = ["ForecastEntry", "forecast", "median"]

MEDIAN_SAMPLE = 10
TWO = 2


@dataclass(frozen=True)
class ForecastEntry:
    """One predicted run: which job, for which run date, and when it should start.

    ``via`` is the upstream job that decided the start of a dependency-only
    job, and ``None`` for scheduled jobs.
    """

    job: str
    run_date: date
    expected_start: datetime
    expected_duration: timedelta | None
    via: str | None


def median(durations: Sequence[timedelta] | None) -> timedelta | None:
    """Return the median of the newest ``MEDIAN_SAMPLE`` durations, or ``None`` if none."""
    if not durations:
        return None
    ordered = sorted(durations[:MEDIAN_SAMPLE])
    mid = len(ordered) // TWO
    if len(ordered) % TWO:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / TWO


def _require_aware(name: str, value: datetime) -> None:
    """Raise ``ValueError`` if ``value`` is a naive datetime."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be timezone-aware, got {value.isoformat()}"
        raise ValueError(msg)


def _end_of(entry: ForecastEntry) -> datetime:
    """Return when the entry is expected to finish (its start when unknown)."""
    return entry.expected_start + (entry.expected_duration or timedelta(0))


def _same_day(job: JobDef) -> set[str]:
    """Return the jobs ``job`` depends on for its own run date (offset 0)."""
    return {d.job for d in job.depends_on if d.run_date_offset == 0}


def _order(followers: list[JobDef]) -> list[JobDef]:
    """Return the dependency-only jobs, upstreams first; same-day cycles are dropped.

    Only offset-0 dependencies order the jobs. One on an earlier run date
    (``run_date: -1``, even on the job itself) looks at a run that is
    already over, so it never makes a loop: ``load`` needing its own
    yesterday is forecast like any other follower.
    """
    names = {job.name for job in followers}
    waiting = {
        job.name: {up for up in _same_day(job) if up in names and up != job.name}
        for job in followers
    }
    # A same-day self-dependency is a cycle too.
    cyclic = {job.name for job in followers if job.name in _same_day(job)}
    ready = deque(job for job in followers if not waiting[job.name] and job.name not in cyclic)
    ordered: list[JobDef] = []
    while ready:
        job = ready.popleft()
        ordered.append(job)
        for other in followers:
            if job.name in waiting[other.name]:
                waiting[other.name].discard(job.name)
                if not waiting[other.name] and other.name not in cyclic:
                    ready.append(other)
    return ordered


def _follower_entries(
    config: Config,
    job: JobDef,
    latest: Mapping[str, Mapping[date, ForecastEntry]],
    durations: Mapping[str, Sequence[timedelta]],
) -> list[ForecastEntry]:
    """Return one entry per run date for which any upstream has an entry."""
    calendar = config.calendar_for(job)
    run_dates: set[date] = set()
    for dep in job.depends_on:
        for day in latest.get(dep.job, {}):
            run_date = calendar.shift(day, -dep.run_date_offset)
            if calendar.shift(run_date, dep.run_date_offset) == day:
                run_dates.add(run_date)
    duration = median(durations.get(job.name))
    entries: list[ForecastEntry] = []
    for run_date in sorted(run_dates):
        best: ForecastEntry | None = None
        for dep in job.depends_on:
            upstream = latest.get(dep.job, {}).get(calendar.shift(run_date, dep.run_date_offset))
            if upstream is not None and (best is None or _end_of(upstream) > _end_of(best)):
                best = upstream
        if best is not None:
            entries.append(ForecastEntry(job.name, run_date, _end_of(best), duration, best.job))
    return entries


def forecast(
    config: Config,
    start: datetime,
    end: datetime,
    *,
    durations: Mapping[str, Sequence[timedelta]] | None = None,
) -> list[ForecastEntry]:
    """Return the predicted runs for the window, sorted by ``(expected_start, job)``.

    Scheduled jobs get one entry per firing after ``start`` and up to ``end``.
    Dependency-only jobs get one entry per run date, starting when the
    slowest upstream is expected to finish. Disabled and manual jobs are left
    out. ``durations`` maps a job to its past durations, newest first.

    Raises:
        ValueError: If ``start`` or ``end`` is naive.

    """
    _require_aware("start", start)
    _require_aware("end", end)
    known = durations or {}
    entries: list[ForecastEntry] = []
    latest: dict[str, dict[date, ForecastEntry]] = {}

    def add(job: JobDef, new: list[ForecastEntry]) -> None:
        """Record entries and remember the latest one per run date."""
        entries.extend(new)
        by_date = latest.setdefault(job.name, {})
        for entry in new:
            current = by_date.get(entry.run_date)
            if current is None or entry.expected_start >= current.expected_start:
                by_date[entry.run_date] = entry

    enabled = [job for job in config.jobs.values() if job.enabled and not job.manual]
    for job in enabled:
        if job.schedules:
            duration = median(known.get(job.name))
            firings = firings_between(job, config.calendar_for(job), start, end)
            add(
                job,
                [
                    ForecastEntry(job.name, f.run_date, f.scheduled_at, duration, None)
                    for f in firings
                ],
            )
    for job in _order([j for j in enabled if j.dependency_only]):
        add(job, _follower_entries(config, job, latest, known))
    return sorted(entries, key=lambda e: (e.expected_start, e.job))
