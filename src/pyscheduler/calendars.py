"""Business calendars: which days count as working days.

Think of school days. Weekends are off, and so are snow days, but a
Saturday catch-up lesson can be added. A ``BusinessCalendar`` answers
"is this a school day?" and "which school day came before?".
"""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

MAX_SEARCH_DAYS = 3660
DAYS_IN_WEEK = 7


@dataclass(frozen=True)
class BusinessCalendar:
    """A named set of working weekdays, holidays and extra working days.

    Weekday numbers follow Python: 0 is Monday, 6 is Sunday.
    """

    name: str
    weekdays: frozenset[int]
    holidays: frozenset[date]
    extra_days: frozenset[date]

    def is_business_day(self, d: date) -> bool:
        """Return whether ``d`` is a business day (extra days always are)."""
        if d in self.extra_days:
            return True
        return d.weekday() in self.weekdays and d not in self.holidays

    def previous(self, d: date) -> date:
        """Return the nearest business day strictly before ``d``.

        Raises:
            ValueError: If none exists within ``MAX_SEARCH_DAYS``.

        """
        return self._search(d, -1)

    def next(self, d: date) -> date:
        """Return the nearest business day strictly after ``d``.

        Raises:
            ValueError: If none exists within ``MAX_SEARCH_DAYS``.

        """
        return self._search(d, 1)

    def shift(self, d: date, n: int) -> date:
        """Move ``d`` by ``n`` business days; zero returns ``d`` unchanged."""
        result = d
        for _ in range(abs(n)):
            result = self._search(result, 1 if n > 0 else -1)
        return result

    def nth_of_month(self, year: int, month: int, n: int) -> date | None:
        """Return the n-th business day of a month, or ``None`` if there are too few.

        Positive ``n`` counts from the start (1 is the first), negative from
        the end (-1 is the last).

        Raises:
            ValueError: If ``n`` is 0.

        """
        if n == 0:
            msg = "n must not be 0"
            raise ValueError(msg)
        days_in_month = calendar.monthrange(year, month)[1]
        days = [date(year, month, day) for day in range(1, days_in_month + 1)]
        business = [d for d in days if self.is_business_day(d)]
        index = n - 1 if n > 0 else n
        if -len(business) <= index < len(business):
            return business[index]
        return None

    def _search(self, d: date, step: int) -> date:
        """Walk from ``d`` by ``step`` days until a business day is found."""
        for offset in range(1, MAX_SEARCH_DAYS + 1):
            try:
                candidate = d + timedelta(days=step * offset)
            except OverflowError:
                break
            if self.is_business_day(candidate):
                return candidate
        msg = f"no business day within {MAX_SEARCH_DAYS} days of {d.isoformat()} in calendar {self.name!r}"
        raise ValueError(msg)


ALL_DAYS = BusinessCalendar("none", frozenset(range(DAYS_IN_WEEK)), frozenset(), frozenset())


def merge_calendars(
    name: str, base: BusinessCalendar, included: Sequence[BusinessCalendar]
) -> BusinessCalendar:
    """Return ``base`` renamed, with holidays united with those of ``included``.

    The weekdays and extra days of ``base`` are kept; those of the included
    calendars are not merged.
    """
    holidays = set(base.holidays)
    for cal in included:
        holidays |= cal.holidays
    return BusinessCalendar(name, base.weekdays, frozenset(holidays), base.extra_days)
