"""``run_date_on`` agrees with ``firings_on``, without touching the day cache."""

import random
from dataclasses import dataclass
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.schedule import firings
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.firings import RunDateRule, firings_on, run_date_on
from pyscheduler.schedule.specs import (
    AtSchedule,
    BusinessDaySchedule,
    CronSchedule,
    EverySchedule,
    Schedule,
)

ZONES = ("UTC", "Europe/London", "America/New_York", "Australia/Lord_Howe")
SEEDS = range(1, 16)
DAYS = 400
START = date(2026, 1, 1)
FILTERED_SHARE = 0.3
"""How often a random schedule gets a weekday filter."""
CRON, AT, EVERY, BUSINESS_DAY = range(4)
CALENDAR = BusinessCalendar(
    "uk", frozenset(range(5)), frozenset({date(2026, 12, 25), date(2026, 12, 28)}), frozenset()
)


@dataclass(frozen=True)
class _Job:
    """A tiny job that satisfies the Timed protocol."""

    schedules: tuple[Schedule, ...]
    timezone: ZoneInfo
    run_date_rule: RunDateRule


def _schedule(rng: random.Random) -> Schedule:
    """Return one random schedule of any kind."""
    kind = rng.randrange(BUSINESS_DAY + 1)
    days = (
        frozenset(rng.sample(range(7), rng.randint(1, 7)))
        if rng.random() < FILTERED_SHARE
        else None
    )
    if kind == CRON:
        return CronSchedule(
            CronExpr.parse(f"{rng.randrange(60)} {rng.randrange(24)} * * *"), days=days
        )
    if kind == AT:
        return AtSchedule((time(rng.randrange(24), rng.randrange(60)),), days=days)
    if kind == EVERY:
        step = timedelta(minutes=rng.choice([1, 15, 90]))
        start, end = time(rng.randrange(24), rng.randrange(60)), time(rng.randrange(24))
        return EverySchedule(step, start, end, days=days)
    n = rng.choice([1, 2, -1, -3])
    return BusinessDaySchedule(n, (time(rng.randrange(24)),), days=days)


class TestRunDateOn:
    """The cheap per-day answer matches the full list of firings."""

    @pytest.mark.parametrize("seed", SEEDS)
    def test_matches_firings_on(self, seed: int) -> None:
        """For every day of a year, the run date is that of the day's firings (or none)."""
        rng = random.Random(seed)  # noqa: S311 - test data, not crypto
        job = _Job(
            tuple(_schedule(rng) for _ in range(rng.randint(1, 2))),
            ZoneInfo(rng.choice(ZONES)),
            rng.choice(list(RunDateRule)),
        )
        firings._cached_day.cache_clear()
        for n in range(DAYS):
            day = START + timedelta(days=n)
            found = run_date_on(job, day, CALENDAR)
            assert firings._cached_day.cache_info().currsize == 0
            expected = {f.run_date for f in firings_on(job, day, CALENDAR)}
            firings._cached_day.cache_clear()
            assert ({found} if found is not None else set()) == expected, (day, job)
