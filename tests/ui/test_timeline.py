"""Test the timeline page (``/timeline``): windows, segments, running bars, ghosts and links."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from api_helpers import DEV, START, TODAY, ApiHarness, MakeApi
from ui_helpers import Element, find, seed, stored_run

from pyscheduler.engine.model import Outcome, RunState
from pyscheduler.testing import FakeResult
from pyscheduler.ui.timeline_window import WEEK_DAYS, Zoom, ticks, window_for

OK = 200
BAD_REQUEST = 400
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DASH = "\N{EN DASH}"
LONDON = ZoneInfo("Europe/London")
CLOCK_BACK = date(2026, 10, 25)
"""The day the clocks go back in London: 25 hours long."""
DST_DAY_HOURS = 25
HOUR_TICKS = 12
DAY_TICKS = 24

TIMELINE_JOBS = """
jobs:
  alpha:
    command: run-a
    schedule: manual
  beta:
    command: run-b
    schedule: "30 18 * * *"
  gamma:
    command: run-c
    schedule: manual
"""


def _at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    """Return ``hour:minute`` UTC on ``day``."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _get(h_: ApiHarness, path: str) -> Any:
    """GET ``path`` in development mode."""
    return h_.client.get(path, headers=DEV_HTML)


def _lanes(text: str) -> list[str]:
    """Return the lane job names in drawing order."""
    return [e[1].get("data-job") or "" for e in find(text, "g", **{"class": "lane"})]


def _bars(text: str) -> list[Element]:
    """Return the bar rectangles in drawing order."""
    return [e for e in find(text, "rect") if (e[1].get("class") or "").startswith("bar ")]


def _retried(h_: ApiHarness) -> None:
    """Seed a run of alpha with a failed attempt 02:00-02:10 then a succeeded one 02:20-02:30."""
    run, (first,) = stored_run(
        "alpha", RunState.SUCCEEDED, TODAY, _at(2), took=10 * MINUTE, outcome=Outcome.FAILED
    )
    second = replace(
        first,
        attempt=2,
        started_at=_at(2, 20),
        ended_at=_at(2, 30),
        outcome=Outcome.SUCCEEDED,
    )
    seed(h_, (replace(run, attempt=2), [first, second]))


class TestWindows:
    """The zoom presets cut local time, including on a 25-hour day."""

    def test_zoom_windows(self) -> None:
        """Hour is now plus or minus an hour; day and week start at local midnight."""
        now = _at(12)
        hour = window_for(Zoom.HOUR, TODAY, now, LONDON)
        assert (hour.start, hour.end) == (now - HOUR, now + HOUR)
        day = window_for(Zoom.DAY, TODAY, now, LONDON)
        assert day.start == datetime(2026, 10, 19, tzinfo=LONDON)
        assert day.span == 24 * HOUR
        week = window_for(Zoom.WEEK, TODAY, now, LONDON)
        assert week.end == datetime(2026, 10, 26, tzinfo=LONDON)

    def test_dst_day_is_25_hours(self) -> None:
        """On 2026-10-25 in London the day runs 23:00Z to 00:00Z and has 25 hourly ticks."""
        day = window_for(Zoom.DAY, CLOCK_BACK, _at(12), LONDON)
        assert day.span == DST_DAY_HOURS * HOUR
        assert day.start.astimezone(UTC) == datetime(2026, 10, 24, 23, tzinfo=UTC)
        assert day.end.astimezone(UTC) == datetime(2026, 10, 26, 0, tzinfo=UTC)
        marks = ticks(day, LONDON)
        assert len(marks) == DST_DAY_HOURS
        assert [m.strftime("%H:%M") for m in marks[:3]] == ["00:00", "01:00", "01:00"]
        week = window_for(Zoom.WEEK, CLOCK_BACK, _at(12), LONDON)
        assert week.span == (WEEK_DAYS * 24 + 1) * HOUR
        assert len(ticks(week, LONDON)) == WEEK_DAYS

    def test_window_cap(self) -> None:
        """Week is the largest zoom: seven days, and no other zoom exists."""
        assert {z.value for z in Zoom} == {"hour", "day", "week"}
        week = window_for(Zoom.WEEK, TODAY, _at(12), ZoneInfo("UTC"))
        assert week.span == WEEK_DAYS * 24 * HOUR
        longest = window_for(Zoom.WEEK, CLOCK_BACK, _at(12), LONDON).span
        assert longest <= (WEEK_DAYS * 24 + 1) * HOUR

    def test_ticks_by_zoom(self) -> None:
        """Hour ticks fall every 10 minutes on the clock; day ticks every hour."""
        hour = window_for(Zoom.HOUR, TODAY, _at(12, 5), LONDON)
        marks = ticks(hour, LONDON)
        assert len(marks) == HOUR_TICKS
        assert all(m.minute % 10 == 0 for m in marks)
        assert len(ticks(window_for(Zoom.DAY, TODAY, _at(12), LONDON), LONDON)) == DAY_TICKS

    def test_run_dates_include_the_day_before(self) -> None:
        """The run dates read start one day before the first local day shown."""
        day = window_for(Zoom.DAY, TODAY, _at(12), LONDON)
        assert day.run_dates(LONDON) == [TODAY - timedelta(days=1), TODAY]


class TestPage:
    """The page draws lanes and bars from runs, attempts and the forecast."""

    def test_segments_per_attempt(self, api: MakeApi) -> None:
        """A run with two attempts gives two bars in its lane: failed, then succeeded."""
        h_ = api(TIMELINE_JOBS, tokens=False)
        _retried(h_)

        async def body() -> None:
            """Fetch the day."""
            response = await _get(h_, "/timeline?zoom=day")
            assert response.status == OK
            lane = [b for b in _bars(response.text) if not (b[1]["class"] or "").endswith("ghost")]
            assert [b[1]["class"] for b in lane] == ["bar failed", "bar succeeded"]
            assert [b[2] for b in lane] == [
                f"attempt 1: failed, 02:00:00{DASH}02:10:00",
                f"attempt 2: succeeded, 02:20:00{DASH}02:30:00",
            ]
            assert float(lane[0][1]["x"] or 0) < float(lane[1][1]["x"] or 0)
            assert "alpha" in _lanes(response.text)

        h_.run(body)

    def test_running_extends_to_now(self, api: MakeApi) -> None:
        """A running attempt is drawn from its start to the clock's now, in blue."""
        h_ = api(TIMELINE_JOBS, tokens=False)
        h_.launcher.plan("alpha", FakeResult(duration=3 * HOUR))

        async def body() -> None:
            """Start a three-hour run, move now to 01:00 and fetch."""
            await h_.client.post("/api/jobs/alpha/trigger", headers=DEV)
            await h_.advance_to(_at(1))
            text = (await _get(h_, "/timeline?zoom=day")).text
            (bar,) = (b for b in _bars(text) if b[1]["class"] == "bar running")
            (now,) = find(text, "line", **{"class": "now-line"})
            right = float(bar[1]["x"] or 0) + float(bar[1]["width"] or 0)
            assert right == pytest.approx(float(now[1]["x1"] or 0), abs=0.1)
            assert bar[2] == f"attempt 1: running, 00:00:00{DASH}01:00:00"

        h_.run(body)

    def test_ghost_bars_for_future(self, api: MakeApi) -> None:
        """The scheduled job's next firing today is a ghost bar after the now line."""
        h_ = api(TIMELINE_JOBS, tokens=False)

        async def body() -> None:
            """Fetch the day at midnight."""
            text = (await _get(h_, "/timeline?zoom=day")).text
            (ghost,) = (b for b in _bars(text) if b[1]["class"] == "bar ghost")
            (now,) = find(text, "line", **{"class": "now-line"})
            assert float(ghost[1]["x"] or 0) > float(now[1]["x1"] or 0)
            assert "expected: beta" in ghost[2]
            assert _lanes(text) == ["beta"]
            past = (await _get(h_, "/timeline?zoom=day&date=2026-10-18")).text
            assert "Nothing ran" in past
            assert not find(past, "line", **{"class": "now-line"})

        h_.run(body)

    def test_lanes_sorted_and_links(self, api: MakeApi) -> None:
        """Lanes are by job name; zoom and previous/next links keep the choices."""
        h_ = api(TIMELINE_JOBS, tokens=False)
        seed(
            h_,
            stored_run(
                "gamma", RunState.SUCCEEDED, TODAY, _at(1), took=MINUTE, outcome=Outcome.SUCCEEDED
            ),
            stored_run(
                "alpha", RunState.FAILED, TODAY, _at(2), took=MINUTE, outcome=Outcome.FAILED
            ),
        )

        async def body() -> None:
            """Fetch the week."""
            text = (await _get(h_, "/timeline?zoom=week&date=2026-10-19")).text
            assert _lanes(text) == ["alpha", "beta", "gamma"]
            hrefs = {e[1].get("rel") or e[2]: e[1].get("href") for e in find(text, "a")}
            assert hrefs["next"] == "/timeline?zoom=week&date=2026-10-26"
            assert hrefs["prev"] == "/timeline?zoom=week&date=2026-10-12"
            assert hrefs["hour"] == "/timeline?zoom=hour&date=2026-10-19"
            runs = [
                e[1]["href"]
                for e in find(text, "a")
                if (e[1].get("href") or "").startswith("/runs/")
            ]
            assert len(runs) == 2  # noqa: PLR2004

        h_.run(body)

    @pytest.mark.parametrize("query", ["zoom=month", "date=nope", "zoom=day&date=2026-13-01"])
    def test_bad_query_is_400_html(self, api: MakeApi, query: str) -> None:
        """A bad zoom or date gives a 400 HTML page with the shell."""
        h_ = api(TIMELINE_JOBS, tokens=False)

        async def body() -> None:
            """Fetch the bad page."""
            response = await _get(h_, f"/timeline?{query}")
            assert response.status == BAD_REQUEST
            assert "text/html" in (response.headers.get("Content-Type") or "")
            assert find(response.text, "p", role="alert")

        h_.run(body)

    def test_empty_date_means_today(self, api: MakeApi) -> None:
        """An empty date shows today in the scheduler's zone, as the graph does."""
        h_ = api(TIMELINE_JOBS, tokens=False)

        async def body() -> None:
            """Fetch with an empty date."""
            text = (await _get(h_, "/timeline?date=")).text
            assert START.date().isoformat() in text

        h_.run(body)
