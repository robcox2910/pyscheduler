"""The day a page is asked about, kept inside the calendar the pages can draw.

A page that shows "the day before" and "the day after" does date sums,
and the calendar has two ends: the sums fall off them. Think of a wall
calendar with only the years 1900 to 2999 printed. Asking for a day
beyond the printed pages is refused politely, before any sum is done.
"""

from datetime import date

from pyweb.messages import HttpRequest

from pyscheduler.api.support import BAD_REQUEST, ApiError, query_date

__all__ = ["FIRST_DAY", "LAST_DAY", "in_range", "page_day"]

FIRST_DAY = date(1900, 1, 1)
"""The earliest day a page will show."""

LAST_DAY = date(2999, 12, 31)
"""The latest day a page will show."""


def in_range(day: date) -> bool:
    """Return whether ``day`` is one a page can show (so a link to it would not be a 400)."""
    return FIRST_DAY <= day <= LAST_DAY


def page_day(request: HttpRequest, name: str, today: date) -> date:
    """Return the date in query parameter ``name``; ``today`` when it is missing or empty.

    Raises:
        ApiError: 400 if it is not a date, or is outside ``FIRST_DAY`` to ``LAST_DAY``.

    """
    given = query_date(request, name) if request.query.get(name) else None
    if given is None:
        return today
    if not in_range(given):
        msg = f"{name} must be between {FIRST_DAY.isoformat()} and {LAST_DAY.isoformat()}"
        raise ApiError(BAD_REQUEST, msg)
    return given
