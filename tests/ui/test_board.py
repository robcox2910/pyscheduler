"""Test the job board (``/``): rows, order, filters, counts, actions and the live fragments."""

import time
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_helpers import START, TODAY, ApiHarness, MakeApi, body_of, users_yaml
from ui_helpers import find, parse, seed, sign_in, stored_run

from pyscheduler.engine.model import Outcome, RunState
from pyscheduler.ui.board_view import DASH

OK = 200
BAD_REQUEST = 400
NOT_FOUND = 404
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
OLD = TODAY - timedelta(days=30)
"""A day older than the in-memory window, so its runs come from the store."""
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HISTORY_DAYS = 3
BUSY_JOBS = 120
BUSY_RUNS = 500
BUSY_LIMIT = 1.0
"""Seconds a busy day's board may take to render (Review Focus 4)."""

BOARD_JOBS = """
jobs:
  alpha:
    command: run-a
    schedule: manual
    tags: [finance]
    criticality: critical
  beta:
    command: run-b
    schedule: manual
    tags: [ops]
    criticality: low
  gamma:
    command: run-c
    schedule: manual
"""

ALPHA = f"alpha.{OLD}.1"
BETA_LATE = f"beta.{OLD}.1"
BETA_EARLY = f"beta.{OLD}.2"
GAMMA = f"gamma.{OLD}.1"
ORDER = [ALPHA, BETA_EARLY, BETA_LATE, GAMMA]
"""The critical failure first, then by scheduled time, then by job name."""


def _at(hour: int, day: Any = OLD) -> datetime:
    """Return ``hour`` o'clock UTC on ``day``."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _board(api: MakeApi, users: str | None = None) -> ApiHarness:
    """Return a harness with the three jobs and a day of runs on ``OLD``, plus history."""
    h_ = api(BOARD_JOBS, tokens=False, users=users)
    history = [
        stored_run(
            "beta",
            RunState.SUCCEEDED,
            OLD - timedelta(days=n),
            _at(8, OLD - timedelta(days=n)),
            took=MINUTE,
            outcome=Outcome.SUCCEEDED,
        )
        for n in range(1, HISTORY_DAYS + 1)
    ]
    seed(
        h_,
        stored_run("alpha", RunState.FAILED, OLD, _at(9), took=30 * SECOND, outcome=Outcome.FAILED),
        stored_run(
            "beta",
            RunState.SUCCEEDED,
            OLD,
            _at(8),
            took=2 * MINUTE,
            outcome=Outcome.SUCCEEDED,
        ),
        stored_run(
            "beta", RunState.FAILED, OLD, _at(7), seq=2, took=10 * SECOND, outcome=Outcome.FAILED
        ),
        stored_run("gamma", RunState.SUCCEEDED, OLD, _at(8)),
        *history,
    )
    return h_


def _row_ids(text: str) -> list[str]:
    """Return the run ids of the board's rows, top to bottom."""
    return [
        a["data-run-id"] or "" for tag, a, _ in parse(text) if tag == "tr" and "data-run-id" in a
    ]


def _cells(text: str, run_id: str) -> dict[str, str]:
    """Return the cells of ``run_id``'s row by their ``data-col``, as text."""
    cells: dict[str, str] = {}
    inside = False
    for tag, attrs, inner in parse(text):
        if tag == "tr":
            inside = attrs.get("data-run-id") == run_id
        elif tag == "td" and inside:
            cells[attrs.get("data-col") or ""] = inner.strip()
    assert cells, run_id
    return cells


def _get(h_: ApiHarness, path: str) -> Any:
    """GET ``path`` in development mode (as ``dev``, an admin)."""
    return h_.client.get(path, headers=DEV_HTML)


class TestBoard:
    """The board lists a day's runs in the order the plan fixes."""

    def test_board_lists_runs_for_date(self, api: MakeApi) -> None:
        """The rows are the runs ``/api/runs`` lists for the date, a critical failure first."""
        h_ = _board(api)

        async def body() -> None:
            """Compare the board with the API."""
            board = await _get(h_, f"/?date={OLD}")
            assert board.status == OK
            listed = body_of(await _get(h_, f"/api/runs?run_date={OLD}&limit=1000"))
            assert sorted(_row_ids(board.text)) == sorted(r["run_id"] for r in listed["runs"])
            assert _row_ids(board.text) == ORDER
            cells = _cells(board.text, ALPHA)
            assert cells["job"] == "alpha"
            assert cells["state"] == "failed"
            assert cells["criticality"] == "critical"
            assert cells["scheduled"] == "09:00:00"
            assert cells["start"] == "09:00:00"
            assert cells["end"] == "09:00:30"
            assert cells["duration"] == "30s"
            assert cells["attempt"] == "1"
            assert cells["tags"] == "finance"
            (link,) = find(board.text, "a", href="/jobs/alpha")
            assert link[2] == "alpha"

        h_.run(body)

    def test_filters_and_counts(self, api: MakeApi) -> None:
        """Each filter narrows the rows; the counts always cover the whole day."""
        h_ = _board(api)
        cases = {
            "state=failed": [ALPHA, BETA_EARLY],
            "tag=finance": [ALPHA],
            "criticality=low": [BETA_EARLY, BETA_LATE],
            "q=GAM": [GAMMA],
            "state=failed&tag=ops": [BETA_EARLY],
        }

        async def body() -> None:
            """Try each filter, then the bad ones."""
            for query, expected in cases.items():
                text = (await _get(h_, f"/?date={OLD}&{query}")).text
                assert _row_ids(text) == expected, query
                counts = {
                    attrs.get("data-state"): inner.split()
                    for _, attrs, inner in find(text, "a")
                    if attrs.get("data-state")
                }
                assert counts == {"failed": ["failed", "2"], "succeeded": ["succeeded", "2"]}
            fragment = await _get(h_, f"/ui/fragments/board-counts?date={OLD}&state=failed")
            assert fragment.status == OK
            assert find(fragment.text, "nav", id="board-counts")
            assert "<html" not in fragment.text
            for query in ("date=nope", "state=bogus", "criticality=bogus"):
                assert (await _get(h_, f"/?{query}")).status == BAD_REQUEST, query

        h_.run(body)

    def test_prev_next_and_default_date(self, api: MakeApi) -> None:
        """With no date the board shows today there; prev/next step a day and keep filters."""
        h_ = _board(api)
        today = START.astimezone(h_.config.settings.timezone).date()

        async def body() -> None:
            """Open the board with no date, then with a date and a filter."""
            text = (await _get(h_, "/")).text
            (picker,) = find(text, "input", name="date")
            assert picker[1]["type"] == "date"
            assert picker[1]["value"] == today.isoformat()
            (back,) = find(text, "a", rel="prev")
            (on,) = find(text, "a", rel="next")
            assert back[1]["href"] == f"/?date={today - timedelta(days=1)}"
            assert on[1]["href"] == f"/?date={today + timedelta(days=1)}"
            text = (await _get(h_, f"/?date={OLD}&state=failed")).text
            (back,) = find(text, "a", rel="prev")
            assert back[1]["href"] == f"/?date={OLD - timedelta(days=1)}&state=failed"

        h_.run(body)


class TestActions:
    """Action buttons appear only for operators and carry what app.js posts."""

    def test_viewer_has_no_action_buttons(self, api: MakeApi) -> None:
        """A viewer sees the rows but no buttons and no actions column."""
        h_ = _board(api, users_yaml(vic="viewer"))

        async def body() -> None:
            """Sign in as the viewer."""
            await sign_in(h_, "vic")
            text = (await h_.client.get(f"/?date={OLD}")).text
            assert _row_ids(text) == ORDER
            assert not find(text, "button", type="button")
            assert "Actions" not in [inner for _, _, inner in find(text, "th")]

        h_.run(body)

    def test_operator_actions_use_api(self, api: MakeApi) -> None:
        """An operator's rows carry ``data-action``/``data-run`` buttons fit for each state."""
        h_ = _board(api, users_yaml(ola="operator"))

        async def body() -> None:
            """Sign in as the operator."""
            await sign_in(h_, "ola")
            text = (await h_.client.get(f"/?date={OLD}")).text
            assert "Actions" in [inner for _, _, inner in find(text, "th")]
            actions = {
                run_id: {
                    attrs.get("data-action")
                    for _, attrs, _ in find(text, "button", **{"data-run": run_id})
                }
                for run_id in ORDER
            }
            assert actions[ALPHA] == {"rerun", "mark-success"}
            assert actions[BETA_LATE] == {"rerun", "mark-failed"}

        h_.run(body)


class TestFragments:
    """The row fragment app.js swaps in when a run changes."""

    def test_row_fragment(self, api: MakeApi) -> None:
        """One ``<tr>`` alone; 404 for an unknown run, another date or a filter it misses."""
        h_ = _board(api)

        async def body() -> None:
            """Fetch the fragment for a known and some unknown cases."""
            row = await _get(h_, f"/ui/fragments/board-row/{BETA_LATE}?date={OLD}")
            assert row.status == OK
            assert row.text.startswith("<tr")
            assert row.text.endswith("</tr>")
            assert _row_ids(row.text) == [BETA_LATE]
            for path in (
                f"/ui/fragments/board-row/nope.{OLD}.1?date={OLD}",
                f"/ui/fragments/board-row/{BETA_LATE}?date={TODAY}",
                f"/ui/fragments/board-row/{BETA_LATE}?date={OLD}&state=failed",
            ):
                assert (await _get(h_, path)).status == NOT_FOUND, path

        h_.run(body)

    def test_vs_typical(self, api: MakeApi) -> None:
        """Twice (or under half) the typical duration is flagged; no successes shows a dash."""
        h_ = _board(api)

        async def body() -> None:
            """Read the vs-typical cells."""
            text = (await _get(h_, f"/?date={OLD}")).text
            assert _cells(text, BETA_LATE)["typical"] == "200%"
            assert _cells(text, BETA_EARLY)["typical"] == "17%"
            flagged: list[str] = []
            current = ""
            for tag, attrs, _ in parse(text):
                if tag == "tr":
                    current = attrs.get("data-run-id") or ""
                elif "off-typical" in (attrs.get("class") or ""):
                    assert attrs.get("data-col") == "typical"
                    flagged.append(current)
            assert flagged == [BETA_EARLY, BETA_LATE]
            assert _cells(text, ALPHA)["typical"] == DASH
            assert _cells(text, GAMMA)["duration"] == DASH

        h_.run(body)


class TestBusyBoard:
    """A busy day renders fast, reading durations once."""

    def test_busy_board_is_fast(self, api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
        """500 runs over 120 jobs render in under a second, with one duration read."""
        jobs = "".join(
            f"  job{n:03}:\n    command: run\n    schedule: manual\n" for n in range(BUSY_JOBS)
        )
        h_ = api("jobs:\n" + jobs, tokens=False)
        seed(
            h_,
            *(
                stored_run(
                    f"job{k % BUSY_JOBS:03}",
                    RunState.SUCCEEDED,
                    OLD,
                    _at(0) + k * MINUTE,
                    seq=k // BUSY_JOBS + 1,
                    took=MINUTE,
                    outcome=Outcome.SUCCEEDED,
                )
                for k in range(BUSY_RUNS)
            ),
        )
        calls: list[int] = []
        real = h_.store.recent_durations_for

        def spy(jobs: Collection[str], limit: int = 10) -> dict[str, list[timedelta]]:
            """Count the call and read for real."""
            calls.append(len(jobs))
            return real(jobs, limit)

        monkeypatch.setattr(h_.store, "recent_durations_for", spy)

        async def body() -> None:
            """Time one render of the busy day."""
            began = time.perf_counter()
            board = await _get(h_, f"/?date={OLD}")
            took = time.perf_counter() - began
            assert board.status == OK
            assert len(_row_ids(board.text)) == BUSY_RUNS
            assert took < BUSY_LIMIT, took
            assert calls == [BUSY_JOBS]

        h_.run(body)
