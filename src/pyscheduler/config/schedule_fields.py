"""Read a job's ``schedule`` key into schedule specs.

A schedule can be written three ways: a cron string, a mapping for things
cron can't say neatly, or the word ``manual``. A list mixes them. This
module is the clerk who knows all three handwritings.
"""

from collections.abc import Callable
from datetime import time

from pyscheduler.config.fields import (
    Reader,
    check_keys,
    read_duration,
    read_int,
    read_list,
    read_str,
    read_time,
    read_weekday,
)
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.specs import (
    AtSchedule,
    BusinessDaySchedule,
    CronSchedule,
    EverySchedule,
    Schedule,
)
from pyscheduler.values import parse_time
from pyscheduler.yamlish import Map, Node, Scalar, Seq

MANUAL = "manual"
SCHEDULE_KEYS = ("every", "between", "at", "business_day", "days", "months")


def read_schedule(r: Reader, node: Node) -> tuple[tuple[Schedule, ...], bool]:
    """Return a job's schedules and whether ``manual`` was among them."""
    items = node.items if isinstance(node, Seq) else [node]
    schedules: list[Schedule] = []
    manual = False
    for item in items:
        if isinstance(item, Scalar) and item.value == MANUAL:
            manual = True
            continue
        schedule = _read_one(r, item)
        if schedule is not None:
            schedules.append(schedule)
    return tuple(schedules), manual


def _read_one(r: Reader, node: Node) -> Schedule | None:
    """Read one cron string or schedule mapping."""
    if isinstance(node, Map):
        return _read_mapping(r, node)
    text = read_str(r, node, "schedule")
    if text is None:
        return None
    try:
        return CronSchedule(CronExpr.parse(text))
    except ValueError as exc:
        r.add(node.line, f"schedule: bad cron {text!r}: {exc}")
        return None


def _read_times(r: Reader, node: Node) -> tuple[time, ...] | None:
    """Read ``at``: one time or a list of times."""
    if isinstance(node, Seq):
        times = read_list(r, node, "at", read_time)
        return None if times is None else tuple(times)
    one = read_time(r, node, "at")
    return None if one is None else (one,)


def _read_month(r: Reader, node: Node, what: str) -> int | None:
    """Read a month number 1-12 (range checked by the schedule spec)."""
    return read_int(r, node, what)


def _read_window(r: Reader, node: Node) -> tuple[time, time] | None:
    """Read ``between: "HH:MM-HH:MM"``."""
    text = read_str(r, node, "between")
    if text is None:
        return None
    start, _, end = text.partition("-")
    try:
        return parse_time(start.strip()), parse_time(end.strip())
    except ValueError:
        r.add(node.line, f"between must be HH:MM-HH:MM, got {text!r}")
        return None


type Filter = frozenset[int] | None


def _filters(r: Reader, node: Map) -> tuple[Filter, Filter]:
    """Read the optional ``days`` and ``months`` filters."""
    days = months = None
    if "days" in node.items:
        days = frozenset(read_list(r, node.items["days"], "days", read_weekday) or ())
    if "months" in node.items:
        months = frozenset(read_list(r, node.items["months"], "months", _read_month) or ())
    return days, months


def _build(r: Reader, node: Map, factory: Callable[[], Schedule]) -> Schedule | None:
    """Call ``factory``, noting a ``ValueError`` from the spec as a problem."""
    try:
        return factory()
    except ValueError as exc:
        r.add(node.line, f"schedule: {exc}")
        return None


def _every(r: Reader, node: Map, days: Filter, months: Filter) -> Schedule | None:
    """Read ``{every: 15m, between: "08:00-18:00"}``."""
    items = node.items
    if "at" in items:
        r.add(node.key_lines["at"], "schedule: every cannot be combined with at")
    step = read_duration(r, items["every"], "every")
    if "between" not in items:
        r.add(node.line, 'every needs between, for example between: "08:00-18:00"')
        return None
    window = _read_window(r, items["between"])
    if step is None or window is None:
        return None
    start, end = window
    return _build(r, node, lambda: EverySchedule(step, start, end, days=days, months=months))


def _at(r: Reader, node: Map, days: Filter, months: Filter) -> Schedule | None:
    """Read ``{at: ["09:00", "17:30"]}``, or ``business_day`` with ``at``."""
    items = node.items
    n = read_int(r, items["business_day"], "business_day") if "business_day" in items else None
    if "at" not in items:
        r.add(node.line, 'business_day needs at, for example at: "07:00"')
        return None
    times = _read_times(r, items["at"])
    if times is None:
        return None
    if "business_day" not in items:
        return _build(r, node, lambda: AtSchedule(times, days=days, months=months))
    if n is None:
        return None
    return _build(r, node, lambda: BusinessDaySchedule(n, times, days=days, months=months))


def _read_mapping(r: Reader, node: Map) -> Schedule | None:
    """Read an ``every``, ``at`` or ``business_day`` schedule mapping."""
    before = len(r.problems)
    check_keys(r, node, SCHEDULE_KEYS, "schedule")
    items = node.items
    days, months = _filters(r, node)
    if "every" in items and "business_day" in items:
        r.add(node.line, "schedule may use only one of every or business_day")
        return None
    if "between" in items and "every" not in items:
        r.add(node.key_lines["between"], "between only goes with every")
    if "every" in items:
        schedule = _every(r, node, days, months)
    elif "at" in items or "business_day" in items:
        schedule = _at(r, node, days, months)
    else:
        r.add(node.line, "schedule needs one of every, at or business_day")
        return None
    return schedule if len(r.problems) == before else None
