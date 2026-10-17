"""Schedule specs: the rules that say at which local times a job fires.

A schedule is like a note on the fridge: "every weekday at nine", "every
15 minutes between midnight and three", "on the last working day of the
month at six". Each spec answers one question: given a local day, at which
instants does it ring? The answers are aware UTC ``datetime`` objects, so
the rest of the scheduler never has to think about clocks changing.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.local import resolve_local

DAYS_IN_WEEK = 7
MONTHS_IN_YEAR = 12


class Schedule(Protocol):
    """A rule that turns a local day into the UTC instants it fires at."""

    def instants(self, day: date, tz: ZoneInfo, calendar: BusinessCalendar) -> list[datetime]:
        """Return the UTC instants whose landing day is ``day`` (local).

        The job's calendar filter is not applied here; ``calendar`` is only
        consulted by schedules that count business days.
        """
        ...


def _check_range(name: str, values: frozenset[int] | None, low: int, high: int) -> None:
    """Raise ``ValueError`` if any filter value falls outside ``low``-``high``."""
    if values is None:
        return
    bad = sorted(v for v in values if not low <= v <= high)
    if bad:
        msg = f"{name}: {bad[0]} is out of range {low}-{high}"
        raise ValueError(msg)


@dataclass(frozen=True)
class _Filtered:
    """Shared weekday and month filters on the landing day.

    ``days`` uses Python weekday numbers (0 is Monday); ``months`` runs 1-12.
    ``None`` means no filter.
    """

    days: frozenset[int] | None = field(default=None, kw_only=True)
    months: frozenset[int] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Validate the filters."""
        _check_range("days", self.days, 0, DAYS_IN_WEEK - 1)
        _check_range("months", self.months, 1, MONTHS_IN_YEAR)

    def _lands(self, day: date) -> bool:
        """Return whether ``day`` passes the weekday and month filters."""
        if self.days is not None and day.weekday() not in self.days:
            return False
        return self.months is None or day.month in self.months


def _at_times(day: date, times: tuple[time, ...], tz: ZoneInfo) -> list[datetime]:
    """Resolve each local time on ``day``, dropping duplicates (gap collapses)."""
    return sorted({resolve_local(day, t, tz) for t in times})


@dataclass(frozen=True)
class CronSchedule(_Filtered):
    """Fire at a cron expression's times on the days it matches."""

    cron: CronExpr

    def instants(self, day: date, tz: ZoneInfo, calendar: BusinessCalendar) -> list[datetime]:
        """Return the cron's instants on ``day``."""
        del calendar
        if not self._lands(day) or not self.cron.matches_day(day):
            return []
        return _at_times(day, self.cron.times(), tz)


@dataclass(frozen=True)
class AtSchedule(_Filtered):
    """Fire at fixed local times every day."""

    times: tuple[time, ...]

    def instants(self, day: date, tz: ZoneInfo, calendar: BusinessCalendar) -> list[datetime]:
        """Return the fixed times on ``day``."""
        del calendar
        if not self._lands(day):
            return []
        return _at_times(day, self.times, tz)


@dataclass(frozen=True)
class EverySchedule(_Filtered):
    """Fire every ``step`` of real elapsed time between ``start`` and ``end``.

    The window includes its end. If ``start`` is after ``end`` the window
    crosses midnight and closes on the next day, but every firing in it
    belongs to the day it opened. Steps are measured in real time, so when
    the clocks go back the repeated hour is stepped through again.
    """

    step: timedelta
    start: time
    end: time

    def __post_init__(self) -> None:
        """Validate the step and the filters."""
        super().__post_init__()
        if self.step <= timedelta(0):
            msg = f"step must be positive, got {self.step}"
            raise ValueError(msg)

    def instants(self, day: date, tz: ZoneInfo, calendar: BusinessCalendar) -> list[datetime]:
        """Return the instants of the window opened on ``day``."""
        del calendar
        if not self._lands(day):
            return []
        end_day = day if self.start <= self.end else day + timedelta(days=1)
        current = resolve_local(day, self.start, tz)
        last = resolve_local(end_day, self.end, tz)
        out: list[datetime] = []
        while current <= last:
            out.append(current)
            current += self.step
        return out


@dataclass(frozen=True)
class BusinessDaySchedule(_Filtered):
    """Fire at ``times`` on the n-th business day of each month.

    Positive ``n`` counts from the start of the month, negative from the end
    (-1 is the last business day).
    """

    n: int
    times: tuple[time, ...]

    def __post_init__(self) -> None:
        """Validate ``n`` and the filters."""
        super().__post_init__()
        if self.n == 0:
            msg = "n must not be 0"
            raise ValueError(msg)

    def instants(self, day: date, tz: ZoneInfo, calendar: BusinessCalendar) -> list[datetime]:
        """Return the times on ``day`` if it is the n-th business day of its month."""
        if not self._lands(day) or calendar.nth_of_month(day.year, day.month, self.n) != day:
            return []
        return _at_times(day, self.times, tz)
