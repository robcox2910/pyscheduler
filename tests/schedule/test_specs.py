"""Tests for schedule specs: cron, every, at and business-day schedules."""

import itertools
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.specs import (
    AtSchedule,
    BusinessDaySchedule,
    CronSchedule,
    EverySchedule,
)

UTC_ZONE = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")
UK_BUSINESS = BusinessCalendar("uk-business", frozenset(range(5)), frozenset(), frozenset())
FALL_BACK = date(2026, 10, 25)
EVERY_FALL_BACK_COUNT = 17
WINDOW_FIRINGS = 5
SATURDAY = 5
OCTOBER = 10


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Return an aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestCronSchedule:
    """A cron schedule fires at its times on matching days."""

    def test_non_matching_day_is_empty(self) -> None:
        """A day the cron does not match gives no instants."""
        sched = CronSchedule(CronExpr.parse("0 9 * * mon"))
        assert sched.instants(date(2026, 10, 20), UTC_ZONE, ALL_DAYS) == []
        assert sched.instants(date(2026, 10, 19), UTC_ZONE, ALL_DAYS) == [_utc(2026, 10, 19, 9)]

    def test_gap_times_collapse(self) -> None:
        """Every time inside the spring gap resolves to the same instant."""
        sched = CronSchedule(CronExpr.parse("*/15 1 * * *"))
        got = sched.instants(date(2027, 3, 28), LONDON, ALL_DAYS)
        assert got == [_utc(2027, 3, 28, 1)]


class TestFilters:
    """Days and months filters look at the landing day."""

    def test_days_filter(self) -> None:
        """Only listed weekdays fire."""
        sched = AtSchedule((time(9),), days=frozenset({SATURDAY}))
        assert sched.instants(date(2026, 10, 17), UTC_ZONE, ALL_DAYS) == [_utc(2026, 10, 17, 9)]
        assert sched.instants(date(2026, 10, 18), UTC_ZONE, ALL_DAYS) == []

    def test_months_filter(self) -> None:
        """Only listed months fire."""
        sched = AtSchedule((time(9),), months=frozenset({OCTOBER}))
        assert sched.instants(date(2026, 10, 1), UTC_ZONE, ALL_DAYS) != []
        assert sched.instants(date(2026, 11, 1), UTC_ZONE, ALL_DAYS) == []

    @pytest.mark.parametrize(
        ("days", "months"),
        [(frozenset({7}), None), (None, frozenset({0})), (None, frozenset({13}))],
    )
    def test_bad_filters(self, days: frozenset[int] | None, months: frozenset[int] | None) -> None:
        """Out-of-range filter values are rejected."""
        with pytest.raises(ValueError, match="out of range"):
            AtSchedule((time(9),), days=days, months=months)


class TestEverySchedule:
    """An every schedule steps in real time across its window."""

    def test_every_elapsed_across_fall_back(self) -> None:
        """The repeated hour adds four extra firings."""
        sched = EverySchedule(timedelta(minutes=15), time(0), time(3))
        got = sched.instants(FALL_BACK, LONDON, ALL_DAYS)
        assert len(got) == EVERY_FALL_BACK_COUNT
        assert got[0] == _utc(2026, 10, 24, 23)
        assert got[-1] == _utc(2026, 10, 25, 3)
        steps = {b - a for a, b in itertools.pairwise(got)}
        assert steps == {timedelta(minutes=15)}

    def test_every_window_crosses_midnight(self) -> None:
        """A window from 22:00 to 02:00 belongs to the day it opened."""
        sched = EverySchedule(timedelta(hours=1), time(22), time(2))
        d = date(2026, 10, 19)
        got = sched.instants(d, UTC_ZONE, ALL_DAYS)
        assert len(got) == WINDOW_FIRINGS
        assert [g.hour for g in got] == [22, 23, 0, 1, 2]
        assert got[0] == _utc(2026, 10, 19, 22)
        assert got[-1] == _utc(2026, 10, 20, 2)
        nxt = sched.instants(d + timedelta(days=1), UTC_ZONE, ALL_DAYS)
        assert nxt[0] == _utc(2026, 10, 20, 22)

    def test_end_not_on_step(self) -> None:
        """The window end is a limit, not a firing, when the step misses it."""
        sched = EverySchedule(timedelta(minutes=40), time(9), time(10))
        got = sched.instants(date(2026, 10, 19), UTC_ZONE, ALL_DAYS)
        assert got == [_utc(2026, 10, 19, 9), _utc(2026, 10, 19, 9, 40)]

    @pytest.mark.parametrize("step", [timedelta(0), timedelta(minutes=-5)])
    def test_bad_step(self, step: timedelta) -> None:
        """The step must be positive."""
        with pytest.raises(ValueError, match="step"):
            EverySchedule(step, time(0), time(1))


class TestBusinessDaySchedule:
    """A business-day schedule fires on the n-th business day of the month."""

    def test_business_day_schedule(self) -> None:
        """The last business day of October 2026 is Friday the 30th."""
        sched = BusinessDaySchedule(-1, (time(18),))
        start = date(2026, 10, 1)
        fired = [
            start + timedelta(days=i)
            for i in range(31)
            if sched.instants(start + timedelta(days=i), UTC_ZONE, UK_BUSINESS)
        ]
        assert fired == [date(2026, 10, 30)]

    def test_zero_rejected(self) -> None:
        """N of zero has no meaning."""
        with pytest.raises(ValueError, match="n must not be 0"):
            BusinessDaySchedule(0, (time(18),))
