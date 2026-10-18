"""Tests for business calendars."""

from datetime import date

import pytest

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar, merge_calendars

MON_FRI = frozenset(range(5))
NO_DATES: frozenset[date] = frozenset()
LAST_BUSINESS_DAY_OCT = date(2026, 10, 30)
TOO_MANY = 25
THREE = 3


def _uk() -> BusinessCalendar:
    """Return a Monday-to-Friday calendar with the 2026 Christmas holidays."""
    return BusinessCalendar(
        "uk",
        MON_FRI,
        frozenset({date(2026, 12, 25), date(2026, 12, 28), date(2027, 1, 1)}),
        NO_DATES,
    )


class TestBusinessCalendar:
    """A calendar knows which days are school days."""

    def test_uk_christmas_2026(self) -> None:
        """Previous and next skip weekends and holidays."""
        cal = _uk()
        assert cal.previous(date(2026, 12, 29)) == date(2026, 12, 24)
        assert cal.next(date(2026, 12, 24)) == date(2026, 12, 29)

    def test_extra_day_counts(self) -> None:
        """A Saturday in extra_days is a business day."""
        sat = date(2026, 10, 17)
        cal = BusinessCalendar("x", MON_FRI, NO_DATES, frozenset({sat}))
        assert cal.is_business_day(sat)
        assert not _uk().is_business_day(sat)

    def test_holiday_is_not_business_day(self) -> None:
        """A holiday on a weekday is not a business day."""
        assert not _uk().is_business_day(date(2026, 12, 25))

    def test_shift(self) -> None:
        """Shift steps over business days; zero leaves the date alone."""
        cal = _uk()
        assert cal.shift(date(2026, 10, 19), -1) == date(2026, 10, 16)
        sunday = date(2026, 10, 18)
        assert cal.shift(sunday, 0) == sunday
        assert cal.shift(date(2026, 10, 16), 1) == date(2026, 10, 19)
        assert cal.shift(date(2026, 10, 19), -THREE) == date(2026, 10, 14)

    def test_nth_of_month(self) -> None:
        """Count business days from either end of the month."""
        cal = _uk()
        assert cal.nth_of_month(2026, 10, 1) == date(2026, 10, 1)
        assert cal.nth_of_month(2026, 10, -1) == LAST_BUSINESS_DAY_OCT
        assert cal.nth_of_month(2026, 10, TOO_MANY) is None
        assert cal.nth_of_month(2026, 10, -TOO_MANY) is None

    def test_nth_of_month_zero_raises(self) -> None:
        """Zero is not a valid position."""
        with pytest.raises(ValueError, match="n must not be 0"):
            _uk().nth_of_month(2026, 10, 0)

    def test_empty_calendar_raises(self) -> None:
        """A calendar with no weekdays has no business days to find."""
        cal = BusinessCalendar("empty", frozenset(), NO_DATES, NO_DATES)
        with pytest.raises(ValueError, match="no business day"):
            cal.previous(date(2026, 10, 17))
        with pytest.raises(ValueError, match="no business day"):
            cal.next(date(2026, 10, 17))


class TestAllDaysAndMerge:
    """The all-days calendar and calendar merging."""

    def test_all_days(self) -> None:
        """Every day counts in the none calendar."""
        assert ALL_DAYS.is_business_day(date(2026, 10, 18))
        assert ALL_DAYS.previous(date(2026, 10, 18)) == date(2026, 10, 17)

    def test_merge_unions_holidays(self) -> None:
        """Holidays are united; base weekdays and extra days are kept."""
        sat = date(2026, 10, 17)
        base = BusinessCalendar("a", MON_FRI, frozenset({date(2026, 1, 1)}), frozenset({sat}))
        other = BusinessCalendar("b", frozenset(range(7)), frozenset({date(2026, 5, 4)}), NO_DATES)
        merged = merge_calendars("m", base, [other])
        assert merged.name == "m"
        assert merged.weekdays == MON_FRI
        assert merged.holidays == frozenset({date(2026, 1, 1), date(2026, 5, 4)})
        assert merged.extra_days == frozenset({sat})
