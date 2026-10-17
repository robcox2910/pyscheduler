"""Small value parsers: durations, clock times, dates and names.

Think of these as the bouncers at the door of the config file. Each one
checks that a piece of text looks right and hands back a proper Python
value, or says clearly what is wrong.
"""

import re
from datetime import date, time, timedelta

_DURATION = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", re.ASCII)
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)", re.ASCII)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)


def parse_duration(text: str) -> timedelta:
    """Parse a duration such as ``90s``, ``1h30m`` or ``1d2h3m4s``.

    Units are ``d``, ``h``, ``m`` and ``s``; each may appear once, in that
    order. The total must be greater than zero.

    Raises:
        ValueError: If the text is not a valid, positive duration.

    """
    match = _DURATION.fullmatch(text)
    if match is None or not text:
        msg = f"invalid duration {text!r}: use units like 90s, 15m, 1h30m or 1d"
        raise ValueError(msg)
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    try:
        result = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except OverflowError as exc:
        msg = f"invalid duration {text!r}: too large"
        raise ValueError(msg) from exc
    if result <= timedelta(0):
        msg = f"invalid duration {text!r}: must be greater than zero"
        raise ValueError(msg)
    return result


def parse_time(text: str) -> time:
    """Parse a clock time written ``HH:MM`` (00-23, 00-59).

    Raises:
        ValueError: If the text is not a valid time.

    """
    match = _TIME.fullmatch(text)
    if match is None:
        msg = f"invalid time {text!r}: use HH:MM, for example 07:05"
        raise ValueError(msg)
    return time(int(match.group(1)), int(match.group(2)))


def parse_date(text: str) -> date:
    """Parse an ISO date written ``YYYY-MM-DD``.

    Raises:
        ValueError: If the text is not a valid date.

    """
    if _DATE.fullmatch(text) is None:
        msg = f"invalid date {text!r}: use YYYY-MM-DD"
        raise ValueError(msg)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        msg = f"invalid date {text!r}: {exc}"
        raise ValueError(msg) from exc


def check_name(text: str) -> None:
    """Check that a job, calendar or pool name is 1-64 of ``A-Za-z0-9_-``.

    Raises:
        ValueError: If the name is not allowed.

    """
    if _NAME.fullmatch(text) is None:
        msg = f"invalid name {text!r}: use 1-64 letters, digits, underscores or dashes"
        raise ValueError(msg)
