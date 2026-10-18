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
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
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
    "run_date_on",
]

MAX_SEARCH_DAYS = 1464
ONE_DAY = timedelta(days=1)
LATEST = datetime.max.replace(tzinfo=UTC)
DAY_CACHE_SIZE = 64
"""How many days of firings are remembered (a busy schedule has thousands a day)."""


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


def _compute_day(
    schedules: tuple[Schedule, ...],
    tz: ZoneInfo,
    rule: RunDateRule,
    day: date,
    calendar: BusinessCalendar,
) -> tuple[Firing, ...]:
    """Return the sorted, deduplicated firings landing on ``day``."""
    if not calendar.is_business_day(day):
        return ()
    instants: set[datetime] = set()
    for schedule in schedules:
        instants.update(schedule.instants(day, tz, calendar))
    if not instants:
        return ()
    run_date = calendar.previous(day) if rule is RunDateRule.PREVIOUS_BUSINESS_DAY else day
    return tuple(Firing(instant, run_date) for instant in sorted(instants))


_cached_day = lru_cache(maxsize=DAY_CACHE_SIZE)(_compute_day)


def _day(job: Timed, day: date, calendar: BusinessCalendar) -> tuple[Firing, ...]:
    """Return one day's firings, memoized when the job's parts are hashable.

    Chained ``next_firing`` calls ask about the same days again and again,
    so remembering each day's answer makes them cheap.
    """
    args = (job.schedules, job.timezone, job.run_date_rule, day, calendar)
    try:
        return _cached_day(*args)
    except TypeError:  # an unhashable custom schedule: compute without the cache
        return _compute_day(*args)


def firings_on(job: Timed, day: date, calendar: BusinessCalendar) -> list[Firing]:
    """Return the job's firings whose landing day is ``day``, sorted.

    The schedules' instants are united and deduplicated by instant. Nothing
    fires unless ``day`` is a business day of ``calendar``. The run date is
    ``day`` itself, or the business day before it for
    ``previous_business_day``.

    This is the per-landing-day view. It may include an instant that the
    previous day's midnight-crossing window also produces. Use
    ``firings_between`` or ``next_firing`` for the deduplicated stream, which
    is canonical: there, a shared instant fires once, with the earlier
    landing day's run date.
    """
    return list(_day(job, day, calendar))


def run_date_on(job: Timed, day: date, calendar: BusinessCalendar) -> date | None:
    """Return the run date of the firings landing on ``day``, or ``None`` if none land.

    Every firing of one landing day has the same run date, so this is all
    a run-date analysis needs. It never lists the instants and never
    touches the day cache, so even an every-minute job costs little per day.

    Raises:
        ValueError: If the calendar has no business day before ``day``
            (``previous_business_day``).

    """
    if not calendar.is_business_day(day):
        return None
    if not any(s.lands_on(day, job.timezone, calendar) for s in job.schedules):
        return None
    if job.run_date_rule is RunDateRule.PREVIOUS_BUSINESS_DAY:
        return calendar.previous(day)
    return day


def _require_aware(name: str, value: datetime) -> None:
    """Raise ``ValueError`` if ``value`` is a naive datetime."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be timezone-aware, got {value.isoformat()}"
        raise ValueError(msg)


def _after(firings: tuple[Firing, ...], start: datetime) -> tuple[Firing, ...]:
    """Return the firings strictly after ``start`` (the tuple is sorted)."""
    return firings[bisect_right(firings, start, key=lambda f: f.scheduled_at) :]


def firings_between(
    job: Timed, calendar: BusinessCalendar, start: datetime, end: datetime
) -> Iterator[Firing]:
    """Return an iterator over the job's firings after ``start`` and up to ``end``.

    ``start`` is exclusive and ``end`` inclusive. Firings come in strictly
    increasing order, one local day at a time, computed only as they are
    asked for. An instant produced by two landing days is yielded once, with
    the earlier landing day's run date.

    Raises:
        ValueError: If ``start`` or ``end`` is naive.

    """
    _require_aware("start", start)
    _require_aware("end", end)
    if any(s.crosses_midnight for s in job.schedules):
        return _merged(job, calendar, start, end)
    return _plain(job, calendar, start, end)


def _days(job: Timed, start: datetime, end: datetime, *, day_before: bool) -> Iterator[date]:
    """Yield the local days from ``start``'s (or the day before) to ``end``'s."""
    day = start.astimezone(job.timezone).date()
    if day_before and day > date.min:
        day -= ONE_DAY
    last_day = end.astimezone(job.timezone).date()
    while day <= last_day:
        yield day
        if day == date.max:
            return
        day += ONE_DAY


def _plain(
    job: Timed, calendar: BusinessCalendar, start: datetime, end: datetime
) -> Iterator[Firing]:
    """Yield firings when no window crosses midnight.

    Each day's firings then lie between that day's local midnight and the
    next, so days come out already in order. Only a gap spanning midnight
    can make two days share an instant, hence the ``emitted`` check.
    """
    emitted = start
    for day in _days(job, start, end, day_before=False):
        for firing in _after(_day(job, day, calendar), emitted):
            if firing.scheduled_at > end:
                return
            emitted = firing.scheduled_at
            yield firing


def _merged(
    job: Timed, calendar: BusinessCalendar, start: datetime, end: datetime
) -> Iterator[Firing]:
    """Yield firings when a window may cross midnight.

    Scanning starts the local day before ``start`` so a window opened
    yesterday still counts. A crossing window can fire after the next day's
    first firings, so firings wait in a heap until no later day could fire
    before them: no day fires before its own local midnight.
    """
    pending: list[Firing] = []
    emitted = start
    # Every kept firing is <= end, which is before the midnight after end's
    # local day, so the heap is drained by the time the days run out.
    for day in _days(job, start, end, day_before=True):
        for firing in _after(_day(job, day, calendar), start):
            if firing.scheduled_at > end:
                break
            heapq.heappush(pending, firing)
        horizon = resolve_local(day + ONE_DAY, time(0), job.timezone) if day < date.max else None
        while pending and (horizon is None or pending[0].scheduled_at < horizon):
            firing = heapq.heappop(pending)
            if firing.scheduled_at > emitted:
                emitted = firing.scheduled_at
                yield firing


def next_firing(job: Timed, calendar: BusinessCalendar, after: datetime) -> Firing | None:
    """Return the first firing strictly after ``after``.

    Returns ``None`` if nothing fires within ``MAX_SEARCH_DAYS`` days, which
    means the job never fires (for example, on 30 February).

    Raises:
        ValueError: If ``after`` is naive.

    """
    _require_aware("after", after)
    try:
        end = after + timedelta(days=MAX_SEARCH_DAYS)
    except OverflowError:
        end = LATEST
    return next(firings_between(job, calendar, after, end), None)
