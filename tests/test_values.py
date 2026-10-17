"""Tests for the duration, time, date and name helpers."""

from datetime import date, time, timedelta

import pytest

from pyscheduler.values import check_name, parse_date, parse_duration, parse_time

MAX_NAME = 64
SECONDS_90 = 90


class TestParseDuration:
    """Durations are written like 1h30m."""

    def test_parse_duration_cases(self) -> None:
        """Valid durations parse and bad ones raise."""
        assert parse_duration("1h30m") == timedelta(minutes=90)
        assert parse_duration("1d") == timedelta(days=1)
        assert parse_duration("90s") == timedelta(seconds=SECONDS_90)
        assert parse_duration("15m") == timedelta(minutes=15)
        assert parse_duration("2h") == timedelta(hours=2)
        assert parse_duration("1d2h3m4s") == timedelta(days=1, hours=2, minutes=3, seconds=4)
        for bad in ["", "0", "0s", "-5m", "5x", "5", "m5", "1m1h", "1h1h", " 1h", "1h ", "1.5h"]:
            with pytest.raises(ValueError, match="duration"):
                parse_duration(bad)


class TestParseTime:
    """Times are HH:MM."""

    def test_parse_time_bounds(self) -> None:
        """Out-of-range and sloppy times raise."""
        assert parse_time("07:05") == time(7, 5)
        assert parse_time("23:59") == time(23, 59)
        assert parse_time("00:00") == time(0, 0)
        for bad in ["24:00", "7:5", "12:60", "", "12:00:00", "ab:cd"]:
            with pytest.raises(ValueError, match="time"):
                parse_time(bad)


class TestParseDate:
    """Dates are ISO YYYY-MM-DD."""

    def test_parse_date(self) -> None:
        """ISO dates parse and other shapes raise."""
        assert parse_date("2026-10-17") == date(2026, 10, 17)
        for bad in ["20261017", "2026-13-01", "17/10/2026", "", "2026-1-1"]:
            with pytest.raises(ValueError, match="date"):
                parse_date(bad)


class TestCheckName:
    """Names are 1-64 characters of letters, digits, underscore, dash."""

    def test_check_name(self) -> None:
        """Good names pass and bad names raise."""
        check_name("load-warehouse_2")
        check_name("a" * MAX_NAME)
        for bad in ["a.b", "", "a" * (MAX_NAME + 1), "é", "a\n", "a b"]:
            with pytest.raises(ValueError, match="name"):
                check_name(bad)
