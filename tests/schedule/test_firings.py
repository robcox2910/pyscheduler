"""Tests for firings, time-zone resolution and run dates."""

import itertools
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.firings import (
    Firing,
    RunDateRule,
    firings_between,
    firings_on,
    next_firing,
    resolve_local,
)
from pyscheduler.schedule.specs import AtSchedule, CronSchedule, EverySchedule, Schedule

UTC_ZONE = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
UK_BUSINESS = BusinessCalendar("uk-business", frozenset(range(5)), frozenset(), frozenset())
ZONES = ("UTC", "Europe/London", "America/New_York", "Australia/Sydney", "Asia/Kolkata")
CHAIN = 200
SEEDS = range(1, 21)
WEEK_START = date(2026, 10, 19)
DAYS_IN_WEEK = 7
HALF = 0.5


@dataclass(frozen=True)
class _Job:
    """A tiny job that satisfies the Timed protocol."""

    schedules: tuple[Schedule, ...]
    timezone: ZoneInfo = UTC_ZONE
    run_date_rule: RunDateRule = RunDateRule.SAME_DAY


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Return an aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _cron(text: str, tz: ZoneInfo = UTC_ZONE, rule: RunDateRule = RunDateRule.SAME_DAY) -> _Job:
    """Return a job with one cron schedule."""
    return _Job((CronSchedule(CronExpr.parse(text)),), tz, rule)


class TestResolveLocal:
    """Local wall times become UTC instants."""

    def test_plain(self) -> None:
        """An ordinary time converts with the day's offset."""
        assert resolve_local(date(2026, 7, 1), time(9), LONDON) == _utc(2026, 7, 1, 8)

    def test_gap(self) -> None:
        """A time in the spring gap moves to the first instant after it."""
        assert resolve_local(date(2027, 3, 28), time(1, 30), LONDON) == _utc(2027, 3, 28, 1)

    def test_fold(self) -> None:
        """A repeated time takes its first occurrence."""
        assert resolve_local(date(2026, 10, 25), time(1, 30), LONDON) == _utc(2026, 10, 25, 0, 30)

    def test_midnight_gap(self) -> None:
        """Santiago skips midnight; it resolves to the moment of the jump."""
        santiago = ZoneInfo("America/Santiago")
        jump = _utc(2026, 9, 6, 4)
        assert resolve_local(date(2026, 9, 6), time(0), santiago) == jump
        assert resolve_local(date(2026, 9, 6), time(0, 30, 0, 500), santiago) == jump

    def test_result_is_utc(self) -> None:
        """The result is aware and in UTC."""
        assert resolve_local(date(2026, 7, 1), time(9), NEW_YORK).tzinfo is UTC


class TestFiringsOn:
    """Firings for one local day."""

    def test_london_fall_back_once(self) -> None:
        """01:30 fires once on the day the clocks go back."""
        job = _cron("30 1 * * *", LONDON)
        assert [f.scheduled_at for f in firings_on(job, date(2026, 10, 25), ALL_DAYS)] == [
            _utc(2026, 10, 25, 0, 30)
        ]

    def test_london_spring_gap(self) -> None:
        """01:30 in the gap fires at the first instant after it."""
        job = _cron("30 1 * * *", LONDON)
        assert [f.scheduled_at for f in firings_on(job, date(2027, 3, 28), ALL_DAYS)] == [
            _utc(2027, 3, 28, 1)
        ]

    def test_new_york_dst(self) -> None:
        """New York's gap and fold behave the same way."""
        spring = firings_on(_cron("30 2 * * *", NEW_YORK), date(2027, 3, 14), ALL_DAYS)
        assert [f.scheduled_at for f in spring] == [_utc(2027, 3, 14, 7)]
        fall = firings_on(_cron("30 1 * * *", NEW_YORK), date(2026, 11, 1), ALL_DAYS)
        assert [f.scheduled_at for f in fall] == [_utc(2026, 11, 1, 5, 30)]

    def test_previous_business_day_monday_for_friday(self) -> None:
        """Monday's run is for Friday; weekends do not fire."""
        job = _cron("0 2 * * *", LONDON, RunDateRule.PREVIOUS_BUSINESS_DAY)
        got = {
            WEEK_START + timedelta(days=i): [
                f.run_date for f in firings_on(job, WEEK_START + timedelta(days=i), UK_BUSINESS)
            ]
            for i in range(DAYS_IN_WEEK)
        }
        assert got == {
            date(2026, 10, 19): [date(2026, 10, 16)],
            date(2026, 10, 20): [date(2026, 10, 19)],
            date(2026, 10, 21): [date(2026, 10, 20)],
            date(2026, 10, 22): [date(2026, 10, 21)],
            date(2026, 10, 23): [date(2026, 10, 22)],
            date(2026, 10, 24): [],
            date(2026, 10, 25): [],
        }

    def test_same_day_run_date(self) -> None:
        """The default rule uses the landing day."""
        got = firings_on(_cron("0 2 * * *"), WEEK_START, UK_BUSINESS)
        assert got == [Firing(_utc(2026, 10, 19, 2), WEEK_START)]

    def test_union_dedupes(self) -> None:
        """Two schedules at the same instant fire once."""
        job = _Job((CronSchedule(CronExpr.parse("0 9 * * *")), AtSchedule((time(9),))))
        assert len(firings_on(job, WEEK_START, ALL_DAYS)) == 1

    def test_union_sorted(self) -> None:
        """Firings from several schedules come back in order."""
        job = _Job((AtSchedule((time(18),)), AtSchedule((time(6),))))
        got = [f.scheduled_at.hour for f in firings_on(job, WEEK_START, ALL_DAYS)]
        assert got == [6, 18]


class TestFiringsBetween:
    """Firings over a span of instants."""

    def test_bounds(self) -> None:
        """Start is exclusive and end inclusive."""
        job = _cron("0 * * * *")
        got = list(firings_between(job, ALL_DAYS, _utc(2026, 10, 19, 1), _utc(2026, 10, 19, 3)))
        assert [f.scheduled_at.hour for f in got] == [2, 3]

    def test_window_from_day_before(self) -> None:
        """A window opened yesterday still yields its after-midnight firings."""
        job = _Job((EverySchedule(timedelta(hours=1), time(22), time(2)),))
        got = list(firings_between(job, ALL_DAYS, _utc(2026, 10, 20, 0, 30), _utc(2026, 10, 20, 3)))
        assert [(f.scheduled_at.hour, f.run_date) for f in got] == [
            (1, date(2026, 10, 19)),
            (2, date(2026, 10, 19)),
        ]

    def test_interleaves_across_days(self) -> None:
        """A late window from yesterday merges with today's early firings."""
        job = _Job(
            (EverySchedule(timedelta(hours=1), time(22), time(2)), AtSchedule((time(0, 30),)))
        )
        got = list(
            firings_between(job, ALL_DAYS, _utc(2026, 10, 19, 23, 30), _utc(2026, 10, 20, 3))
        )
        assert [f.scheduled_at for f in got] == [
            _utc(2026, 10, 20, 0),
            _utc(2026, 10, 20, 0, 30),
            _utc(2026, 10, 20, 1),
            _utc(2026, 10, 20, 2),
        ]

    def test_dedupes_across_days(self) -> None:
        """An instant from yesterday's window and today's schedule fires once."""
        job = _Job((EverySchedule(timedelta(hours=1), time(22), time(1)), AtSchedule((time(0),))))
        got = list(
            firings_between(job, ALL_DAYS, _utc(2026, 10, 19, 23, 30), _utc(2026, 10, 20, 0))
        )
        assert got == [Firing(_utc(2026, 10, 20), date(2026, 10, 19))]

    def test_is_lazy(self) -> None:
        """A huge span yields its first firing without computing the rest."""
        job = _cron("* * * * *")
        it = firings_between(job, ALL_DAYS, _utc(2026, 1, 1), _utc(9000, 1, 1))
        assert next(it).scheduled_at == _utc(2026, 1, 1, 0, 1)

    def test_empty_when_end_before_start(self) -> None:
        """A backwards span yields nothing."""
        job = _cron("0 * * * *")
        assert list(firings_between(job, ALL_DAYS, _utc(2026, 10, 19), _utc(2026, 10, 18))) == []


def _random_job(rng: random.Random) -> _Job:
    """Return a job with a random cron or every schedule in a random zone."""
    tz = ZoneInfo(rng.choice(ZONES))
    schedules: list[Schedule] = []
    for _ in range(rng.randint(1, 2)):
        if rng.random() < HALF:
            minutes = ",".join(str(m) for m in sorted(rng.sample(range(60), rng.randint(1, 3))))
            hours = ",".join(str(h) for h in sorted(rng.sample(range(24), rng.randint(1, 4))))
            schedules.append(CronSchedule(CronExpr.parse(f"{minutes} {hours} * * *")))
        else:
            step = timedelta(minutes=rng.choice([7, 15, 30, 45, 90]))
            start = time(rng.randrange(24), rng.randrange(60))
            end = time(rng.randrange(24), rng.randrange(60))
            schedules.append(EverySchedule(step, start, end))
    return _Job(tuple(schedules), tz)


class TestNextFiring:
    """Next firing agrees with firings between."""

    @pytest.mark.parametrize("seed", SEEDS)
    def test_property_next_equals_between(self, seed: int) -> None:
        """Chained next_firing calls equal firings_between, strictly increasing."""
        rng = random.Random(seed)  # noqa: S311 - test data, not crypto
        job = _random_job(rng)
        # Start near a DST change to stress fold and gap handling.
        start = _utc(2026, 10, 20) + timedelta(minutes=rng.randrange(60 * 24 * 10))
        chain: list[Firing] = []
        after = start
        for _ in range(CHAIN):
            f = next_firing(job, ALL_DAYS, after)
            assert f is not None
            chain.append(f)
            after = f.scheduled_at
        assert all(a.scheduled_at < b.scheduled_at for a, b in itertools.pairwise(chain))
        assert list(firings_between(job, ALL_DAYS, start, chain[-1].scheduled_at)) == chain

    def test_never_fires(self) -> None:
        """30 February never comes."""
        assert next_firing(_cron("0 0 30 2 *"), ALL_DAYS, _utc(2026, 10, 19)) is None

    def test_end_of_time(self) -> None:
        """Searching near the last representable day stops cleanly."""
        job = _cron("0 9 * * *")
        last = next_firing(job, ALL_DAYS, _utc(9999, 12, 30))
        assert last == Firing(_utc(9999, 12, 30, 9), date(9999, 12, 30))
        assert next_firing(job, ALL_DAYS, _utc(9999, 12, 31, 10)) is None

    def test_start_of_time(self) -> None:
        """Searching from the first representable day works."""
        job = _cron("0 9 * * *")
        got = list(firings_between(job, ALL_DAYS, _utc(1, 1, 1), _utc(1, 1, 2, 9)))
        assert [f.run_date for f in got] == [date(1, 1, 1), date(1, 1, 2)]

    def test_next_simple(self) -> None:
        """The next firing is strictly after the given instant."""
        f = next_firing(_cron("0 9 * * *"), ALL_DAYS, _utc(2026, 10, 19, 9))
        assert f == Firing(_utc(2026, 10, 20, 9), date(2026, 10, 20))
