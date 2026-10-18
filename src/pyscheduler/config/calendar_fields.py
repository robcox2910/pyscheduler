"""Read the ``calendars:`` section: weekdays, holidays, extra days and includes.

A calendar may ``include`` others: it then has their holidays too (its
own weekdays and extra days stay). Includes are followed all the way, so
if ``a`` includes ``b`` and ``b`` includes ``c``, ``a`` has the holidays
of all three. A cycle (``a`` includes ``b`` includes ``a``) stops the
walk; validation reports it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.config.fields import (
    Reader,
    check_keys,
    read_date,
    read_list,
    read_map,
    read_seq,
    read_str,
    read_weekday,
)
from pyscheduler.config.model import SourceRef
from pyscheduler.yamlish import Node

__all__ = ["CALENDAR_KEYS", "DEFAULT_WEEKDAYS", "RawCalendar", "reachable", "read_calendar"]

CALENDAR_KEYS = ("weekdays", "holidays", "extra_days", "include")
DEFAULT_WEEKDAYS = frozenset(range(5))


@dataclass(frozen=True)
class RawCalendar:
    """A calendar as written, before its includes are merged in."""

    calendar: BusinessCalendar
    includes: tuple[tuple[str, int], ...]
    source: SourceRef


def read_calendar(r: Reader, name: str, node: Node, line: int) -> RawCalendar | None:
    """Read one calendar definition."""
    mapping = read_map(r, node, f"calendar {name}")
    if mapping is None:
        return None
    check_keys(r, mapping, CALENDAR_KEYS, f"calendar {name}")
    items = mapping.items
    weekdays = DEFAULT_WEEKDAYS
    if "weekdays" in items:
        weekdays = frozenset(read_list(r, items["weekdays"], "weekdays", read_weekday) or ())

    def dates(key: str) -> frozenset[date]:
        """Read a list of ISO dates."""
        if key not in items:
            return frozenset()
        return frozenset(read_list(r, items[key], key, read_date) or ())

    includes: list[tuple[str, int]] = []
    if "include" in items:
        for item in read_seq(r, items["include"], "include") or []:
            other = read_str(r, item, "include")
            if other is not None:
                includes.append((other, item.line))
    cal = BusinessCalendar(name, weekdays, dates("holidays"), dates("extra_days"))
    return RawCalendar(cal, tuple(includes), SourceRef(r.file, line))


def reachable(name: str, raw: Mapping[str, RawCalendar]) -> list[str]:
    """Return every calendar ``name`` includes, directly or through others, in visit order.

    A visited set stops at cycles (validation reports those); unknown
    names are skipped (reported by the caller); ``name`` itself is left out.
    """
    seen = {name}
    order: list[str] = []
    todo = [other for other, _ in reversed(raw[name].includes)]
    while todo:
        other = todo.pop()
        if other in seen or other not in raw:
            continue
        seen.add(other)
        order.append(other)
        todo.extend(nested for nested, _ in reversed(raw[other].includes))
    return order
