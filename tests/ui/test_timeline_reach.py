"""Test which runs the timeline reads, the ``unknown`` bar, and the cap on ghost bars."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from api_helpers import DEV, START, ApiHarness, MakeApi
from ui_helpers import Element, find, seed, stored_run

from pyscheduler.engine.model import RunState
from pyscheduler.ui.timeline import GHOST_CAP, LABEL_W, PLOT_W
from pyscheduler.ui.timeline_window import Zoom, window_for

OK = 200
DEV_HTML = {**DEV, "Accept": "text/html"}
NEW_YORK = ZoneInfo("America/New_York")
MONDAY = date(2026, 10, 19)
FRIDAY = date(2026, 10, 16)
TEN_MINUTES = timedelta(minutes=10)
WEEK_BACK = 7
NEAR_END = 0.99
"""The merged band reaches at least this far along the plot (the last minute of the week)."""

NEW_YORK_JOBS = """
  timezone: America/New_York
jobs:
  pbd:
    command: run-pbd
    schedule: manual
  tokyo:
    command: run-tokyo
    schedule: manual
"""
"""A scheduler in New York (the fixture's ``scheduler:`` block continues here)."""

BUSY_JOBS = """
jobs:
  ticker:
    command: run-ticker
    schedule: {every: 1m, between: "00:00-23:59"}
"""
"""A job due every minute: about 10,000 forecast entries in a week."""


def _ny(day: date, hour: int) -> datetime:
    """Return ``hour``:00 New York time on ``day``."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=NEW_YORK)


def _get(h_: ApiHarness, path: str) -> Any:
    """GET ``path`` in development mode, as a browser."""
    return h_.client.get(path, headers=DEV_HTML)


def _bars(text: str) -> list[Element]:
    """Return the bar rectangles in drawing order."""
    return [e for e in find(text, "rect") if (e[1].get("class") or "").startswith("bar ")]


class TestReach:
    """Runs whose run date is a week before or a day after the shown days are read too."""

    def test_window_run_dates(self) -> None:
        """A day window reads seven run dates before it and one after."""
        window = window_for(Zoom.DAY, MONDAY, START, NEW_YORK)
        found = window.run_dates(NEW_YORK)
        assert found[0] == MONDAY - timedelta(days=WEEK_BACK)
        assert found[-1] == MONDAY + timedelta(days=1)
        assert len(found) == WEEK_BACK + 1 + 1

    def test_monday_run_for_friday_and_tokyo_tomorrow(self, api: MakeApi) -> None:
        """Monday 02:00's run for Friday and a 21:00 run dated tomorrow both get bars."""
        h = api(NEW_YORK_JOBS, tokens=False)
        friday_run = stored_run("pbd", RunState.SUCCEEDED, FRIDAY, _ny(MONDAY, 2), took=TEN_MINUTES)
        tomorrow = MONDAY + timedelta(days=1)
        tokyo_run = stored_run(
            "tokyo", RunState.SUCCEEDED, tomorrow, _ny(MONDAY, 21), took=TEN_MINUTES
        )
        seed(h, friday_run, tokyo_run)

        async def body() -> None:
            """Draw Monday's timeline."""
            response = await _get(h, f"/timeline?zoom=day&date={MONDAY.isoformat()}")
            assert response.status == OK
            lanes = [e[1].get("data-job") for e in find(response.text, "g", **{"class": "lane"})]
            assert lanes == ["pbd", "tokyo"]
            assert len(_bars(response.text)) == len(lanes)

        h.run(body)

    def test_outside_the_window_still_dropped(self, api: MakeApi) -> None:
        """A run read for its date but whose attempt is outside the window gets no bar."""
        h = api(NEW_YORK_JOBS, tokens=False)
        seed(h, stored_run("pbd", RunState.SUCCEEDED, FRIDAY, _ny(FRIDAY, 2), took=TEN_MINUTES))

        async def body() -> None:
            """Draw Monday's timeline: Friday's own run is not on it."""
            response = await _get(h, f"/timeline?zoom=day&date={MONDAY.isoformat()}")
            assert _bars(response.text) == []

        h.run(body)


class TestUnknown:
    """An attempt with no end and no outcome, whose run is not running, is ``unknown``."""

    def test_unknown_kind(self, api: MakeApi) -> None:
        """The bar's class is ``unknown``, not ``running``."""
        h = api(tokens=False)
        run, (attempt,) = stored_run(
            "adhoc", RunState.FAILED, MONDAY, START + timedelta(hours=1), took=TEN_MINUTES
        )
        seed(h, (run, [replace(attempt, ended_at=None, outcome=None)]))

        async def body() -> None:
            """Draw the day."""
            response = await _get(h, f"/timeline?zoom=day&date={MONDAY.isoformat()}")
            ((_, attrs, tip),) = [
                b for b in _bars(response.text) if not (b[1]["class"] or "").endswith("ghost")
            ]
            assert attrs["class"] == "bar unknown"
            assert "unknown" in tip

        h.run(body)


class TestGhostCap:
    """A lane draws at most ``GHOST_CAP`` ghosts, then one merged band for the rest."""

    def test_every_minute_for_a_week(self, api: MakeApi) -> None:
        """An every-minute job over a week gives the cap plus one band reaching the last."""
        h = api(BUSY_JOBS, tokens=False)

        async def body() -> None:
            """Draw next week's timeline."""
            later = (START + timedelta(days=1)).date()
            response = await _get(h, f"/timeline?zoom=week&date={later.isoformat()}")
            assert response.status == OK
            ghosts = [b for b in _bars(response.text) if (b[1]["class"] or "").endswith("ghost")]
            assert len(ghosts) == GHOST_CAP + 1
            band = ghosts[-1]
            assert "more expected" in band[2]
            right = float(band[1]["x"] or 0) + float(band[1]["width"] or 0)
            assert right >= LABEL_W + NEAR_END * PLOT_W
            assert float(band[1]["x"] or 0) > float(ghosts[GHOST_CAP - 1][1]["x"] or 0)

        h.run(body)
