"""The stretch of time the timeline shows, and where its ticks fall.

A window is the **piece of tape measure** you hold up to the day: an hour
either side of now, one local day, or one local week. Days are cut at the
scheduler's own midnights, so on the day the clocks change a "day" is 23
or 25 hours long and the window is exactly that long, never an hour out.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

__all__ = ["HOUR", "WEEK_DAYS", "Window", "Zoom", "ticks", "window_for"]

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK_DAYS = 7
"""The longest window: seven local days."""

TEN_MINUTES = timedelta(minutes=10)
_TEN = 10


class Zoom(StrEnum):
    """How much time the timeline shows."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"


@dataclass(frozen=True)
class Window:
    """A stretch of time from ``start`` up to ``end`` (aware, in the scheduler's zone)."""

    zoom: Zoom
    start: datetime
    end: datetime
    day: date
    """The local date the window is anchored on (today for the hour zoom)."""

    @property
    def span(self) -> timedelta:
        """Return the true length of the window (25 hours for a day the clocks go back)."""
        return self.end.astimezone(UTC) - self.start.astimezone(UTC)

    def fraction(self, moment: datetime) -> float:
        """Return how far along the window ``moment`` is (0 at the start, 1 at the end)."""
        return (moment.astimezone(UTC) - self.start.astimezone(UTC)) / self.span

    def contains(self, moment: datetime) -> bool:
        """Return whether ``moment`` is inside the window (the end included)."""
        return self.start <= moment <= self.end

    def run_dates(self, zone: ZoneInfo) -> list[date]:
        """Return the run dates to read: each local day touched, and the day before the first."""
        first = self.start.astimezone(zone).date()
        last = (self.end - timedelta(microseconds=1)).astimezone(zone).date()
        return [first + timedelta(days=n) for n in range(-1, (last - first).days + 1)]


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    """Return local midnight at the start of ``day`` in ``zone``."""
    return datetime.combine(day, time(), tzinfo=zone)


def window_for(zoom: Zoom, day: date, now: datetime, zone: ZoneInfo) -> Window:
    """Return the window for ``zoom``: the hour zoom is centred on ``now``, the others on ``day``."""
    if zoom is Zoom.HOUR:
        return Window(zoom, now - HOUR, now + HOUR, now.astimezone(zone).date())
    days = 1 if zoom is Zoom.DAY else WEEK_DAYS
    return Window(zoom, _midnight(day, zone), _midnight(day + days * DAY, zone), day)


def ticks(window: Window, zone: ZoneInfo) -> list[datetime]:
    """Return the axis ticks: every 10 minutes, every hour or every local midnight by zoom."""
    found: list[datetime] = []
    if window.zoom is Zoom.WEEK:
        day = window.start.astimezone(zone).date()
        while (moment := _midnight(day, zone)) < window.end:
            found.append(moment)
            day += DAY
        return found
    step = TEN_MINUTES if window.zoom is Zoom.HOUR else HOUR
    start = window.start.astimezone(UTC)
    if window.zoom is Zoom.HOUR:
        local = window.start.astimezone(zone)
        round_down = local.replace(minute=local.minute // _TEN * _TEN, second=0, microsecond=0)
        start = round_down.astimezone(UTC)
        while start < window.start.astimezone(UTC):
            start += step
    moment = start
    while moment < window.end.astimezone(UTC):
        found.append(moment.astimezone(zone))
        moment += step
    return found
