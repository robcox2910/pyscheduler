"""Firings: the exact moments a job is due, and the run date each one is for.

A *firing* is one ring of the alarm: the UTC instant it rings at, and the
*run date* the run is about (the "homework day"). A job may have several
schedules; their firings are merged into one list with no repeats, and the
job's business calendar decides which days count.

The engine, forecast and catch-up all ask the same three questions:

- ``firings_on``: what fires on this local day?
- ``firings_between``: what fires after this instant, up to that one?
- ``next_firing``: what is the very next firing?
"""

import heapq
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.schedule.local import resolve_local
from pyscheduler.schedule.specs import Schedule

__all__ = [
    "MAX_SEARCH_DAYS",
    "Firing",
    "RunDateRule",
    "Timed",
    "firings_between",
    "firings_on",
    "next_firing",
    "resolve_local",
]

MAX_SEARCH_DAYS = 1464
ONE_DAY = timedelta(days=1)
LATEST = datetime.max.replace(tzinfo=UTC)


class RunDateRule(StrEnum):
    """Which date a firing's run is for."""

    SAME_DAY = "same_day"
    PREVIOUS_BUSINESS_DAY = "previous_business_day"


@dataclass(frozen=True, order=True)
class Firing:
    """One firing: a UTC instant and the run date it is for.

    Firings sort by instant, then by run date.
    """

    scheduled_at: datetime
    run_date: date


class Timed(Protocol):
    """Anything with schedules, a time zone and a run-date rule (like a job)."""

    @property
    def schedules(self) -> tuple[Schedule, ...]:
        """Return the job's schedules."""
        ...

    @property
    def timezone(self) -> ZoneInfo:
        """Return the zone the schedules' local times are read in."""
        ...

    @property
    def run_date_rule(self) -> RunDateRule:
        """Return how a firing's run date is chosen."""
        ...


def firings_on(job: Timed, day: date, calendar: BusinessCalendar) -> list[Firing]:
    """Return the job's firings whose landing day is ``day``, sorted.

    The schedules' instants are united and deduplicated by instant. Nothing
    fires unless ``day`` is a business day of ``calendar``. The run date is
    ``day`` itself, or the business day before it for
    ``previous_business_day``.
    """
    if not calendar.is_business_day(day):
        return []
    instants: set[datetime] = set()
    for schedule in job.schedules:
        instants.update(schedule.instants(day, job.timezone, calendar))
    if not instants:
        return []
    if job.run_date_rule is RunDateRule.PREVIOUS_BUSINESS_DAY:
        run_date = calendar.previous(day)
    else:
        run_date = day
    return [Firing(instant, run_date) for instant in sorted(instants)]


def firings_between(
    job: Timed, calendar: BusinessCalendar, start: datetime, end: datetime
) -> Iterator[Firing]:
    """Yield the job's firings after ``start`` and up to ``end``, in order.

    ``start`` is exclusive and ``end`` inclusive. Days are produced lazily,
    one at a time, starting the local day before ``start`` so that a window
    opened yesterday still counts. A window that crosses midnight can fire
    after the next day's first firings, so firings wait in a heap until no
    later day could fire before them. An instant already yielded (from
    another day's window) is not yielded again.
    """
    if end <= start:
        return
    tz = job.timezone
    day = start.astimezone(tz).date()
    if day > date.min:
        day -= ONE_DAY
    last_day = end.astimezone(tz).date()
    pending: list[Firing] = []
    emitted = start
    # Every kept firing is <= end, which is before last_day + 1's midnight, so
    # the heap is drained by the time the loop ends.
    while day <= last_day:
        for firing in firings_on(job, day, calendar):
            if start < firing.scheduled_at <= end:
                heapq.heappush(pending, firing)
        # No later day can fire before its own local midnight.
        horizon = resolve_local(day + ONE_DAY, time(0), tz) if day < date.max else None
        while pending and (horizon is None or pending[0].scheduled_at < horizon):
            firing = heapq.heappop(pending)
            if firing.scheduled_at > emitted:
                emitted = firing.scheduled_at
                yield firing
        if horizon is None:
            break
        day += ONE_DAY


def next_firing(job: Timed, calendar: BusinessCalendar, after: datetime) -> Firing | None:
    """Return the first firing strictly after ``after``.

    Returns ``None`` if nothing fires within ``MAX_SEARCH_DAYS`` days, which
    means the job never fires (for example, on 30 February).
    """
    try:
        end = after + timedelta(days=MAX_SEARCH_DAYS)
    except OverflowError:
        end = LATEST
    return next(firings_between(job, calendar, after, end), None)
