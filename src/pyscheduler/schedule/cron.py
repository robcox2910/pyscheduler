"""Cron expressions: an alarm clock with many dials.

A cron expression has five dials: minute, hour, day-of-month, month and
day-of-week. ``CronExpr.parse("*/15 9-17 * * mon-fri")`` turns the text
into sets of allowed values. ``matches_day`` says whether a calendar day
is allowed and ``times`` lists the times of day to fire on such a day.

A day-of-month or day-of-week field counts as *restricted* unless it is
exactly ``*``. So ``*/1`` is restricted, as in Vixie cron. When both are
restricted a day matches if EITHER matches (the classic cron OR rule);
when only one is restricted only that one applies. The month always
applies. ``?`` is not supported.
"""

import calendar
from dataclasses import dataclass
from datetime import date, time
from functools import lru_cache

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}
FIELD_COUNT = 5
MONTH_NAMES = {
    name: i
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
DAY_NAMES = {name: i for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
SUNDAY_ALT = 7
DAYS_IN_WEEK = 7
MAX_NTH = 5
TIMES_CACHE_SIZE = 256

MINUTE = "minute"
HOUR = "hour"
DAY_OF_MONTH = "day-of-month"
MONTH = "month"
DAY_OF_WEEK = "day-of-week"


def _number(field: str, token: str, names: dict[str, int]) -> int:
    """Return the value of a number or name token."""
    t = token.lower()
    if t in names:
        return names[t]
    if t.isascii() and t.isdigit():
        return int(t)
    msg = f"{field}: invalid value {token!r}"
    raise ValueError(msg)


def _check(field: str, value: int, low: int, high: int) -> None:
    """Raise if ``value`` is outside ``low``..``high``."""
    if not low <= value <= high:
        msg = f"{field}: {value} is out of range {low}-{high}"
        raise ValueError(msg)


def _expand(field: str, item: str, low: int, high: int, names: dict[str, int]) -> set[int]:
    """Expand one list item (``*``, ``a``, ``a-b``, any with ``/step``)."""
    base, slash, step_text = item.partition("/")
    step = 1
    if slash:
        if not (step_text.isascii() and step_text.isdigit()):
            msg = f"{field}: invalid step {step_text!r}"
            raise ValueError(msg)
        step = int(step_text)
        if step < 1:
            msg = f"{field}: step {step} must be positive"
            raise ValueError(msg)
    if base == "*":
        start, end = low, high
    elif "-" in base:
        a, _, b = base.partition("-")
        start, end = _number(field, a, names), _number(field, b, names)
        if field == DAY_OF_WEEK and end == 0 and start > 0:
            end = SUNDAY_ALT
        _check(field, start, low, high)
        _check(field, end, low, high)
        if start > end:
            msg = f"{field}: range {start}-{end} is backwards"
            raise ValueError(msg)
    else:
        start = _number(field, base, names)
        _check(field, start, low, high)
        end = high if slash else start
    return set(range(start, end + 1, step))


def _simple(
    field: str, text: str, low: int, high: int, names: dict[str, int] | None = None
) -> frozenset[int]:
    """Parse a plain comma list field into a frozenset."""
    out: set[int] = set()
    for item in text.split(","):
        if not item:
            msg = f"{field}: empty item in {text!r}"
            raise ValueError(msg)
        out |= _expand(field, item, low, high, names or {})
    return frozenset(out)


@dataclass(frozen=True)
class CronExpr:
    """A parsed five-field cron expression."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    last_day: bool
    months: frozenset[int]
    weekdays: frozenset[int]
    nth_weekdays: frozenset[tuple[int, int]]
    last_weekdays: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, text: str) -> CronExpr:
        """Parse cron text or a macro such as ``@daily``.

        Raises:
            ValueError: If the text is invalid; the message names the field.

        """
        body = text.strip()
        if body.startswith("@"):
            if body not in MACROS:
                msg = f"macro: unknown macro {body!r}"
                raise ValueError(msg)
            body = MACROS[body]
        parts = body.split()
        if len(parts) != FIELD_COUNT:
            msg = f"fields: expected {FIELD_COUNT} fields, got {len(parts)}"
            raise ValueError(msg)
        minute, hour, dom, month, dow = parts
        days, last_day = cls._parse_dom(dom)
        weekdays, nth, last_wd = cls._parse_dow(dow)
        return cls(
            text=text,
            minutes=_simple(MINUTE, minute, 0, 59),
            hours=_simple(HOUR, hour, 0, 23),
            days=days,
            last_day=last_day,
            months=_simple(MONTH, month, 1, 12, MONTH_NAMES),
            weekdays=weekdays,
            nth_weekdays=nth,
            last_weekdays=last_wd,
            dom_restricted=dom != "*",
            dow_restricted=dow != "*",
        )

    @staticmethod
    def _parse_dom(text: str) -> tuple[frozenset[int], bool]:
        """Parse day-of-month, returning the numbers and whether ``L`` appeared."""
        last = False
        rest: list[str] = []
        for item in text.split(","):
            if item.upper() == "L":
                last = True
            else:
                rest.append(item)
        if not rest:
            return frozenset(), last
        return _simple(DAY_OF_MONTH, ",".join(rest), 1, 31), last

    @staticmethod
    def _parse_dow(
        text: str,
    ) -> tuple[frozenset[int], frozenset[tuple[int, int]], frozenset[int]]:
        """Parse day-of-week into plain days, ``#`` pairs and ``L`` days."""
        plain: set[int] = set()
        nth: set[tuple[int, int]] = set()
        last: set[int] = set()
        field = DAY_OF_WEEK
        for item in text.split(","):
            if "#" in item:
                name, _, n_text = item.partition("#")
                n = _number(field, n_text, {})
                if not 1 <= n <= MAX_NTH:
                    msg = f"{field}: occurrence {n} is out of range 1-{MAX_NTH}"
                    raise ValueError(msg)
                nth.add((_single_dow(name), n))
            elif item.lower().endswith("l") and item.lower() not in DAY_NAMES:
                last.add(_single_dow(item[:-1]))
            else:
                plain |= {v % DAYS_IN_WEEK for v in _expand(field, item, 0, 7, DAY_NAMES)}
        return frozenset(plain), frozenset(nth), frozenset(last)

    def times(self) -> tuple[time, ...]:
        """Return every hour and minute combination, sorted (computed once per dial set)."""
        return _times(self.hours, self.minutes)

    def matches_day(self, d: date) -> bool:
        """Return whether ``d`` is allowed by the day, month and weekday rules."""
        if d.month not in self.months:
            return False
        month_len = calendar.monthrange(d.year, d.month)[1]
        dom_ok = d.day in self.days or (self.last_day and d.day == month_len)
        dow = (d.weekday() + 1) % DAYS_IN_WEEK
        dow_ok = (
            dow in self.weekdays
            or (dow, (d.day - 1) // DAYS_IN_WEEK + 1) in self.nth_weekdays
            or (dow in self.last_weekdays and d.day + DAYS_IN_WEEK > month_len)
        )
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True


@lru_cache(maxsize=TIMES_CACHE_SIZE)
def _times(hours: frozenset[int], minutes: frozenset[int]) -> tuple[time, ...]:
    """Return every hour and minute combination, sorted."""
    return tuple(time(h, m) for h in sorted(hours) for m in sorted(minutes))


def _single_dow(token: str) -> int:
    """Return one weekday (0-6, Sunday 0) from a name or number."""
    value = _number(DAY_OF_WEEK, token, DAY_NAMES)
    _check(DAY_OF_WEEK, value, 0, SUNDAY_ALT)
    return value % DAYS_IN_WEEK
