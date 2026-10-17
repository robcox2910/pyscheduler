"""Tests for cron expressions."""

from datetime import date, time

import pytest

from pyscheduler.schedule.cron import CronExpr

EXPECTED_TIMES = 36
SUNDAY = 0


class TestCronExpr:
    """Parse cron text and test days and times."""

    def test_fields(self) -> None:
        """Steps, ranges and names combine."""
        c = CronExpr.parse("*/15 9-17 * * mon-fri")
        assert len(c.times()) == EXPECTED_TIMES
        assert c.times()[0] == time(9, 0)
        assert c.times()[-1] == time(17, 45)
        assert c.matches_day(date(2026, 10, 19))
        assert not c.matches_day(date(2026, 10, 17))
        assert c.text == "*/15 9-17 * * mon-fri"

    def test_or_rule(self) -> None:
        """Both day fields restricted means either may match."""
        c = CronExpr.parse("0 0 13 * fri")
        assert c.matches_day(date(2026, 11, 13))
        assert c.matches_day(date(2026, 10, 16))
        assert not c.matches_day(date(2026, 10, 15))

    def test_single_day_field_applies_alone(self) -> None:
        """An unrestricted day field is ignored."""
        assert not CronExpr.parse("0 0 13 * *").matches_day(date(2026, 10, 16))
        assert not CronExpr.parse("0 0 * * fri").matches_day(date(2026, 10, 15))

    def test_star_slash_one_is_restricted(self) -> None:
        """``*/1`` counts as restricted, as in Vixie cron."""
        c = CronExpr.parse("0 0 */1 * fri")
        assert c.matches_day(date(2026, 10, 15))

    def test_month_always_applies(self) -> None:
        """The month must match even with the OR rule."""
        c = CronExpr.parse("0 0 13 jan fri")
        assert not c.matches_day(date(2026, 10, 16))
        assert c.matches_day(date(2027, 1, 1))

    def test_last_day(self) -> None:
        """``L`` is the last day of the month."""
        c = CronExpr.parse("0 0 L * *")
        assert c.matches_day(date(2027, 2, 28))
        assert not c.matches_day(date(2027, 2, 27))
        assert c.matches_day(date(2028, 2, 29))

    def test_last_day_in_list(self) -> None:
        """``L`` can sit in a list with numbers."""
        c = CronExpr.parse("0 0 1,15,L * *")
        assert c.matches_day(date(2026, 10, 1))
        assert c.matches_day(date(2026, 10, 15))
        assert c.matches_day(date(2026, 10, 31))
        assert not c.matches_day(date(2026, 10, 30))

    def test_nth_weekday(self) -> None:
        """``mon#2`` is the second Monday."""
        c = CronExpr.parse("0 0 * * mon#2")
        days = [d for d in range(1, 32) if c.matches_day(date(2026, 10, d))]
        assert days == [12]

    def test_last_weekday(self) -> None:
        """``friL`` and ``5L`` are the last Friday."""
        for text in ("0 0 * * friL", "0 0 * * 5L"):
            c = CronExpr.parse(text)
            assert c.matches_day(date(2026, 10, 30))
            assert not c.matches_day(date(2026, 10, 23))

    def test_names_and_sunday7(self) -> None:
        """Names are case-insensitive and 0 and 7 are Sunday."""
        assert CronExpr.parse("0 0 * jan *").months == frozenset({1})
        assert CronExpr.parse("0 0 * JAN *").months == frozenset({1})
        assert CronExpr.parse("0 0 * * sun").weekdays == frozenset({SUNDAY})
        assert CronExpr.parse("0 0 * * 7").weekdays == frozenset({SUNDAY})
        assert CronExpr.parse("0 0 * * fri-sun").weekdays == frozenset({5, 6, 0})

    def test_macros(self) -> None:
        """Macros expand but keep the original text."""
        assert CronExpr.parse("@daily").times() == (time(0, 0),)
        assert len(CronExpr.parse("@hourly").times()) == 24  # noqa: PLR2004
        assert CronExpr.parse("@weekly").weekdays == frozenset({SUNDAY})
        assert CronExpr.parse("@monthly").days == frozenset({1})
        yearly = CronExpr.parse("@yearly")
        assert yearly.months == frozenset({1})
        assert yearly.text == "@yearly"

    def test_step_forms(self) -> None:
        """Range steps and ``a/n`` are accepted."""
        assert CronExpr.parse("1-30/10 * * * *").minutes == frozenset({1, 11, 21})
        assert CronExpr.parse("50/5 * * * *").minutes == frozenset({50, 55})

    @pytest.mark.parametrize(
        ("text", "field"),
        [
            ("60 * * * *", "minute"),
            ("* * * *", "fields"),
            ("*/0 * * * *", "minute"),
            ("5-1 * * * *", "minute"),
            ("0 0 L * mon#6", "day-of-week"),
            ("0 0 * * mon#0", "day-of-week"),
            ("0 24 * * *", "hour"),
            ("0 0 0 * *", "day-of-month"),
            ("0 0 * 13 *", "month"),
            ("0 0 * * 8", "day-of-week"),
            ("0 0 ? * *", "day-of-month"),
            ("0 0 * * foo", "day-of-week"),
            ("0 0 * L *", "month"),
            ("0 0 L/2 * *", "day-of-month"),
            ("@never", "macro"),
            ("", "fields"),
        ],
    )
    def test_bad_expressions(self, text: str, field: str) -> None:
        """Bad input raises with the field named."""
        with pytest.raises(ValueError, match=field):
            CronExpr.parse(text)
