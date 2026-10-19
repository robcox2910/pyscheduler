"""The forecast at ``/forecast``: what the scheduler expects to start, and when.

This is the **weather forecast for jobs**: a table of the runs that should
begin between two moments, each with the time it is expected to start, how
long it usually takes, and (for a job that waits for another) which
upstream job it is waiting on. It uses the same window rules as the
``/api/forecast`` endpoint: ``?from=`` and ``?to=`` are ISO 8601 instants
with a time zone, the default is now to 24 hours ahead, and the window is
at most 31 days. Times are shown in the scheduler's own time zone.
"""

from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse

from pyscheduler.api.jobs import MAX_WINDOW, forecast_entries, forecast_window
from pyscheduler.api.serialize import duration_text, instant
from pyscheduler.api.support import ApiError
from pyscheduler.engine.core import Engine
from pyscheduler.schedule.forecast import ForecastEntry
from pyscheduler.ui.html import Safe, h, join
from pyscheduler.ui.layout import page

__all__ = ["add_forecast"]

_DASH = "\N{EN DASH}"
_SHORTCUTS = (("Next 24 hours", timedelta(hours=24)), ("Next 7 days", timedelta(days=7)))


def _row(entry: ForecastEntry, zone: ZoneInfo) -> Safe:
    """Return one table row for a forecast entry."""
    local = entry.expected_start.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    took = _DASH if entry.expected_duration is None else duration_text(entry.expected_duration)
    return h(
        "tr",
        {"data-job": entry.job},
        h(
            "td",
            {"title": instant(entry.expected_start)},
            h("time", {"datetime": instant(entry.expected_start)}, local),
        ),
        h("td", None, h("a", {"href": f"/jobs/{quote(entry.job, safe='')}"}, entry.job)),
        h("td", None, entry.run_date.isoformat()),
        h("td", None, took),
        h("td", None, entry.via or _DASH),
    )


def _shortcuts(now: datetime) -> Safe:
    """Return links to the common windows (they start now, so only ``to`` is set)."""
    return h(
        "p",
        {"class": "board-filters"},
        *(
            h("a", {"href": "/forecast?" + urlencode({"to": (now + span).isoformat()})}, label)
            for label, span in _SHORTCUTS
        ),
        f"(at most {MAX_WINDOW.days} days)",
    )


def _table(entries: list[ForecastEntry], zone: ZoneInfo) -> Safe:
    """Return the forecast table (or a note when nothing is due)."""
    if not entries:
        return h("p", {"class": "empty"}, "No runs are expected in this window.")
    heads = ("Expected start", "Job", "Run date", "Expected duration", "Via")
    return h(
        "table",
        {"class": "forecast"},
        h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, head) for head in heads))),
        h("tbody", None, *(_row(entry, zone) for entry in entries)),
    )


def add_forecast(app: App, engine: Engine) -> None:
    """Add the forecast page (``/forecast``) to ``app``."""

    @app.get("/forecast")
    async def forecast_view(request: HttpRequest) -> HttpResponse:
        """List the runs expected in the asked window (400 page for a bad one)."""
        now = engine.clock.now()
        zone = engine.config.settings.timezone
        try:
            start, end = forecast_window(request, now)
            entries = await forecast_entries(engine, start, end)
        except ApiError as exc:
            body = h("p", {"class": "error", "role": "alert"}, exc.message)
            response = page(request, title="Forecast", body=body, active="forecast")
            response.status = exc.status
            return response
        span = (
            f"{start.astimezone(zone):%Y-%m-%d %H:%M} {_DASH} {end.astimezone(zone):%Y-%m-%d %H:%M}"
        )
        body = join(h("h1", None, f"Forecast · {span}"), _shortcuts(now), _table(entries, zone))
        return page(request, title="Forecast", body=body, active="forecast")

    del forecast_view
