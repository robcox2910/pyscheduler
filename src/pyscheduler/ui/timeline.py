"""The timeline at ``/timeline``: when each job actually ran, and when it should next.

Picture a **railway timetable board**: one row per train (job), time
running left to right. Each attempt is a bar coloured by how it ended
(green succeeded, red failed, blue still running); a run that was retried
shows its attempts side by side. Dashed "ghost" bars further right are the
forecast of what is due, and a vertical line marks now.

``?zoom=hour|day|week`` picks the scale (hour: the last and next hour;
day: one local day; week: seven local days, the most it shows) and
``?date=`` the local day it starts on (today when missing or empty; the
hour zoom is always centred on now). The page is drawn on the server; no
script is needed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from pynet.loop import run_in_thread
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse

from pyscheduler.api.jobs import forecast_entries
from pyscheduler.api.runs import merged_runs
from pyscheduler.api.support import BAD_REQUEST, ApiError
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Run, RunState
from pyscheduler.schedule.forecast import ForecastEntry
from pyscheduler.ui.board import BOARD_LIMIT
from pyscheduler.ui.dates import in_range, page_day
from pyscheduler.ui.html import Safe, h, join
from pyscheduler.ui.layout import page
from pyscheduler.ui.timeline_window import DAY, WEEK_DAYS, Window, Zoom, ticks, window_for

__all__ = ["GHOST_CAP", "Bar", "TimelineQuery", "add_timeline", "bars"]

LABEL_W = 160
PLOT_W = 1000
LANE_H = 30
BAR_H = 18
AXIS_H = 28
MIN_BAR = 3
"""The narrowest a bar is drawn (pixels), so a one-second attempt is still visible."""

GHOST = "ghost"
RUNNING = "running"
UNKNOWN = "unknown"
"""The kind of an attempt with no end and no outcome whose run is not running."""

GHOST_CAP = 200
"""The most ghost bars drawn in one lane; the rest become one merged band."""
_DASH = "\N{EN DASH}"


@dataclass(frozen=True)
class Bar:
    """One bar in a lane: an attempt, or a forecast (``kind`` is ``ghost``)."""

    job: str
    start: datetime
    end: datetime
    kind: str
    """The outcome (``succeeded``, ``failed``...), ``running``, ``unknown`` or ``ghost``."""
    tip: str
    href: str | None


@dataclass(frozen=True)
class TimelineQuery:
    """What the timeline shows: a zoom and the local day it starts on."""

    zoom: Zoom
    day: date

    def link(self) -> str:
        """Return the timeline's address with these choices."""
        return "/timeline?" + urlencode({"zoom": self.zoom.value, "date": self.day.isoformat()})

    def step(self, direction: int) -> TimelineQuery:
        """Return the query one window earlier (-1) or later (+1)."""
        days = WEEK_DAYS if self.zoom is Zoom.WEEK else 1
        return replace(self, day=self.day + direction * days * DAY)


def _query(engine: Engine, request: HttpRequest) -> TimelineQuery:
    """Return the timeline's choices from the query (the day defaults to today there).

    Raises:
        ApiError: 400 for a zoom that is not hour, day or week, or a date that does not parse.

    """
    query = request.query
    text = query.get("zoom") or Zoom.DAY.value
    try:
        zoom = Zoom(text)
    except ValueError:
        names = ", ".join(z.value for z in Zoom)
        raise ApiError(BAD_REQUEST, f"zoom must be one of {names}") from None
    today = engine.clock.now().astimezone(engine.config.settings.timezone).date()
    day = page_day(request, "date", today)
    return TimelineQuery(zoom, day)


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    """Return ``HH:MM:SS`` for ``moment`` in the scheduler's zone."""
    return moment.astimezone(zone).strftime("%H:%M:%S")


def bars(
    runs: Sequence[Run],
    attempts: Mapping[str, Sequence[Attempt]],
    entries: Sequence[ForecastEntry],
    window: Window,
    now: datetime,
    zone: ZoneInfo,
) -> list[Bar]:
    """Return the bars for the window: each attempt that overlaps it, then the ghosts.

    A running attempt ends at ``now``. An attempt with no end that is not
    running is drawn as a sliver at its start, of kind ``unknown`` when it
    has no outcome either. A lane gets at most ``GHOST_CAP`` ghosts (see
    ``_ghosts``).
    """
    found: list[Bar] = []
    for run in runs:
        for attempt in attempts.get(run.run_id, ()):
            running = attempt.ended_at is None and run.state is RunState.RUNNING
            end = attempt.ended_at or (now if running else attempt.started_at)
            if end < window.start or attempt.started_at > window.end:
                continue
            kind = attempt.outcome.value if attempt.outcome else (RUNNING if running else UNKNOWN)
            span = f"{_clock(attempt.started_at, zone)}{_DASH}" + (
                _clock(end, zone) if attempt.ended_at or running else ""
            )
            tip = f"attempt {attempt.attempt}: {kind.replace('_', ' ')}, {span}"
            href = f"/runs/{quote(run.run_id, safe='')}"
            found.append(Bar(run.job, attempt.started_at, end, kind, tip, href))
    found += _ghosts(entries, zone)
    found.sort(key=lambda bar: (bar.job, bar.start, bar.kind))
    return found


def _ghosts(entries: Sequence[ForecastEntry], zone: ZoneInfo) -> list[Bar]:
    """Return a ghost bar per forecast entry, at most ``GHOST_CAP`` per job.

    Past the cap (a job due every minute has 10,000 in a week) one merged
    band covers the rest, from the first left out to the end of the last.
    """
    by_job: dict[str, list[ForecastEntry]] = {}
    for entry in sorted(entries, key=lambda e: (e.job, e.expected_start)):
        by_job.setdefault(entry.job, []).append(entry)
    found: list[Bar] = []
    for job, listed in by_job.items():
        for entry in listed[:GHOST_CAP]:
            tip = (
                f"expected: {job}, run date {entry.run_date}, {_clock(entry.expected_start, zone)}"
            )
            found.append(Bar(job, entry.expected_start, _expected_end(entry), GHOST, tip, None))
        rest = listed[GHOST_CAP:]
        if rest:
            start, end = rest[0].expected_start, max(_expected_end(e) for e in rest)
            span = f"{_clock(start, zone)}{_DASH}{_clock(end, zone)}"
            tip = f"{len(rest)} more expected runs of {job}, {span}"
            found.append(Bar(job, start, end, GHOST, tip, None))
    return found


def _expected_end(entry: ForecastEntry) -> datetime:
    """Return when a forecast entry is expected to end (its start when there is no estimate)."""
    return entry.expected_start + (entry.expected_duration or timedelta(0))


def _x(window: Window, moment: datetime) -> float:
    """Return the horizontal position of ``moment``, kept inside the plot."""
    return LABEL_W + min(max(window.fraction(moment), 0.0), 1.0) * PLOT_W


def _bar(window: Window, bar: Bar, lane: int) -> Safe:
    """Return one bar: a rect with a tip, linked to its run when it has one."""
    left, right = _x(window, bar.start), _x(window, bar.end)
    rect = h(
        "rect",
        {
            "class": f"bar {bar.kind}",
            "x": f"{left:.1f}",
            "y": str(AXIS_H + lane * LANE_H + (LANE_H - BAR_H) // 2),
            "width": f"{max(right - left, MIN_BAR):.1f}",
            "height": str(BAR_H),
            "rx": "3",
        },
        h("title", None, bar.tip),
    )
    if bar.href is None:
        return rect
    return h("a", {"href": bar.href, "class": "bar-link"}, rect)


def _axis(window: Window, zone: ZoneInfo, height: int) -> Safe:
    """Return the axis: a faint line and a local-time label at each tick."""
    parts: list[Safe] = []
    for moment in ticks(window, zone):
        x = f"{_x(window, moment):.1f}"
        label = moment.strftime("%m-%d") if window.zoom is Zoom.WEEK else moment.strftime("%H:%M")
        parts.append(
            h("line", {"class": "tick", "x1": x, "x2": x, "y1": str(AXIS_H - 6), "y2": str(height)})
        )
        parts.append(
            h(
                "text",
                {"class": "tick-label", "x": x, "y": str(AXIS_H - 10), "text-anchor": "middle"},
                label,
            )
        )
    return h("g", {"class": "axis"}, *parts)


def _drawing(window: Window, found: Sequence[Bar], now: datetime, zone: ZoneInfo) -> Safe:
    """Return the ``<svg>``: axis, one lane per job (sorted by name), bars and the now line."""
    names = sorted({bar.job for bar in found})
    height = AXIS_H + len(names) * LANE_H
    width = LABEL_W + PLOT_W
    lanes = [
        h(
            "g",
            {"class": "lane", "data-job": name},
            h(
                "line",
                {
                    "class": "lane-rule",
                    "x1": "0",
                    "x2": str(width),
                    "y1": str(AXIS_H + (n + 1) * LANE_H),
                    "y2": str(AXIS_H + (n + 1) * LANE_H),
                },
            ),
            h(
                "a",
                {"href": f"/jobs/{quote(name, safe='')}", "class": "lane-name"},
                h("title", None, name),
                h("text", {"x": "4", "y": str(AXIS_H + n * LANE_H + LANE_H // 2)}, name),
            ),
            *(_bar(window, bar, n) for bar in found if bar.job == name),
        )
        for n, name in enumerate(names)
    ]
    marker = None
    if window.contains(now):
        x = f"{_x(window, now):.1f}"
        marker = h(
            "line",
            {"class": "now-line", "x1": x, "x2": x, "y1": "0", "y2": str(height)},
            h("title", None, f"now {_clock(now, zone)}"),
        )
    return h(
        "svg",
        {
            "class": "timeline",
            "role": "img",
            "aria-label": "job timeline",
            "width": str(width),
            "height": str(height),
            "viewBox": f"0 0 {width} {height}",
        },
        _axis(window, zone, height),
        *lanes,
        marker,
    )


def _picker(choice: TimelineQuery) -> Safe:
    """Return the zoom links, previous/next links (not on the hour zoom) and a date form."""
    zooms = [
        h(
            "a",
            {
                "href": replace(choice, zoom=zoom).link(),
                "aria-current": "true" if zoom is choice.zoom else None,
            },
            zoom.value,
        )
        for zoom in Zoom
    ]
    moves: list[Safe] = []
    if choice.zoom is not Zoom.HOUR:
        before = choice.step(-1)
        after = choice.step(1)
        if in_range(before.day):
            moves.append(h("a", {"href": before.link(), "rel": "prev"}, "← Previous"))
        if in_range(after.day):
            moves.append(h("a", {"href": after.link(), "rel": "next"}, "Next →"))
    return h(
        "form",
        {"method": "get", "action": "/timeline", "class": "board-filters"},
        *zooms,
        *moves,
        h("input", {"type": "hidden", "name": "zoom", "value": choice.zoom.value}),
        h(
            "input",
            {"type": "date", "name": "date", "value": choice.day.isoformat(), "aria-label": "date"},
        ),
        h("button", {"type": "submit"}, "Show"),
    )


async def _body(engine: Engine, choice: TimelineQuery) -> Safe:
    """Return the timeline page's body for ``choice``."""
    zone = engine.config.settings.timezone
    now = engine.clock.now()
    window = window_for(choice.zoom, choice.day, now, zone)
    runs: list[Run] = []
    for day in window.run_dates(zone):
        runs += await merged_runs(engine, run_date=day, limit=BOARD_LIMIT)
    started = [run.run_id for run in runs if run.attempt]
    attempts = await run_in_thread(engine.store.attempts_for_runs, started)
    ahead = max(window.start, now)
    entries = await forecast_entries(engine, ahead, window.end) if ahead < window.end else []
    found = bars(runs, attempts, entries, window, now, zone)
    first, last = (
        window.start.astimezone(zone),
        (window.end - timedelta(microseconds=1)).astimezone(zone),
    )
    heading = f"Timeline · {first:%Y-%m-%d %H:%M} {_DASH} {last:%Y-%m-%d %H:%M}"
    drawing = (
        h("div", {"class": "timeline-wrap"}, _drawing(window, found, now, zone))
        if found
        else h("p", {"class": "empty"}, "Nothing ran or is due in this window.")
    )
    return join(h("h1", None, heading), _picker(choice), drawing)


def add_timeline(app: App, engine: Engine) -> None:
    """Add the timeline (``/timeline``) to ``app``."""

    @app.get("/timeline")
    async def timeline(request: HttpRequest) -> HttpResponse:
        """Draw the timeline for the asked zoom and date."""
        try:
            choice = _query(engine, request)
            body = await _body(engine, choice)
        except ApiError as exc:
            body = h("p", {"class": "error", "role": "alert"}, exc.message)
            response = page(request, title="Timeline", body=body, active="timeline")
            response.status = exc.status
            return response
        return page(request, title="Timeline", body=body, active="timeline")

    del timeline
