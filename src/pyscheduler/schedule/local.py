"""Turn a local wall-clock time into one UTC instant, even when clocks change.

Twice a year the clocks jump. In spring an hour is skipped (a *gap*): in
London, 01:30 on the last Sunday of March never happens. In autumn an hour
is repeated (a *fold*): 01:30 happens twice. ``resolve_local`` picks one
answer for both cases so an alarm still rings exactly once.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ONE_SECOND = timedelta(seconds=1)


def _offset(instant: datetime, tz: ZoneInfo) -> timedelta | None:
    """Return the UTC offset that ``tz`` uses at ``instant``."""
    return instant.astimezone(tz).utcoffset()


def resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Return the UTC instant of local time ``at`` on ``day`` in ``tz``.

    In a fold the first occurrence (``fold=0``) wins. In a gap the result is
    the first real instant after the gap, which is the moment the clocks
    jumped. Transitions fall on whole seconds, so the search works in
    seconds.
    """
    local = datetime.combine(day, at.replace(tzinfo=None, fold=0), tzinfo=tz)
    early = local.astimezone(UTC)
    if early.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None):
        return early
    # In a gap: fold=0 and fold=1 use the offsets either side of the jump,
    # so the transition lies between the two readings. Binary-search it.
    late = local.replace(fold=1).astimezone(UTC)
    low = min(early, late).replace(microsecond=0)
    high = max(early, late)
    if high.microsecond:
        high = high.replace(microsecond=0) + ONE_SECOND
    before = _offset(low, tz)
    while high - low > ONE_SECOND:
        mid = (low + (high - low) / 2).replace(microsecond=0)
        if _offset(mid, tz) == before:
            low = mid
        else:
            high = mid
    return high
