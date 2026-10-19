"""Test the dependency graph page (``/graph``): states, links, edges, tag filter and focus."""

from datetime import UTC, datetime, timedelta
from typing import Any

from api_helpers import START, TODAY, ApiHarness, MakeApi
from ui_helpers import Element, find, parse, seed, stored_run

from pyscheduler.engine.model import Outcome, RunState, make_run_id

OK = 200
BAD_REQUEST = 400
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
OLD = TODAY - timedelta(days=30)
"""A day older than the in-memory window, so its runs come from the store."""
MINUTE = timedelta(minutes=1)

GRAPH_JOBS = """
jobs:
  extract:
    command: run-extract
    schedule: manual
    tags: [finance]
  load:
    command: run-load
    schedule: manual
    tags: [finance]
    depends_on: [extract]
  report:
    command: run-report
    schedule: manual
    tags: [ops]
    depends_on:
      - {job: load, on: failure}
      - {job: report, run_date: -1}
  audit:
    command: run-audit
    schedule: manual
    depends_on:
      - {job: extract, on: done}
  lonely:
    command: run-lonely
    schedule: manual
"""

EXTRACT_RUN = make_run_id("extract", OLD, 1)
LOAD_FIRST = make_run_id("load", OLD, 1)
LOAD_LATEST = make_run_id("load", OLD, 2)
ALL_JOBS = {"audit", "extract", "load", "lonely", "report"}


def _at(hour: int) -> datetime:
    """Return ``hour`` o'clock UTC on ``OLD``."""
    return datetime(OLD.year, OLD.month, OLD.day, hour, tzinfo=UTC)


def _graph(api: MakeApi) -> ApiHarness:
    """Return a harness with the five jobs; extract and load (twice) ran on ``OLD``."""
    h_ = api(GRAPH_JOBS, tokens=False)
    seed(
        h_,
        stored_run(
            "extract", RunState.SUCCEEDED, OLD, _at(7), took=MINUTE, outcome=Outcome.SUCCEEDED
        ),
        stored_run("load", RunState.FAILED, OLD, _at(8), took=MINUTE, outcome=Outcome.FAILED),
        stored_run(
            "load", RunState.SUCCEEDED, OLD, _at(9), seq=2, took=MINUTE, outcome=Outcome.SUCCEEDED
        ),
    )
    return h_


def _get(h_: ApiHarness, path: str) -> Any:
    """GET ``path`` in development mode (as ``dev``, an admin)."""
    return h_.client.get(path, headers=DEV_HTML)


def _nodes(text: str) -> dict[str, Element]:
    """Return the graph's node links by job name."""
    return {e[1].get("data-job") or "": e for e in find(text, "a") if "data-job" in e[1]}


def _classes(element: Element) -> set[str]:
    """Return the element's classes."""
    return set((element[1].get("class") or "").split())


def _edges(text: str) -> dict[tuple[str, str], set[str]]:
    """Return each edge's classes by ``(up, down)``."""
    return {
        (e[1].get("data-up") or "", e[1].get("data-down") or ""): _classes(e)
        for e in find(text, "path")
        if "data-up" in e[1]
    }


def _box(text: str, job: str) -> set[str]:
    """Return the classes of the box (rect) inside ``job``'s node."""
    seen = False
    for tag, attrs, _ in parse(text):
        if tag == "a":
            seen = attrs.get("data-job") == job
        elif tag == "rect" and seen:
            return set((attrs.get("class") or "").split())
    raise AssertionError(job)


class TestGraphPage:
    """The graph shows every job, its state for the date, and the dependencies."""

    def test_nodes_show_state_and_link(self, api: MakeApi) -> None:
        """A job's box takes its latest run's state and links to it; no run links to the job."""
        h_ = _graph(api)

        async def body() -> None:
            """Open the graph for ``OLD``."""
            page = await _get(h_, f"/graph?run_date={OLD}")
            assert page.status == OK
            nodes = _nodes(page.text)
            assert set(nodes) == ALL_JOBS
            assert nodes["extract"][1]["href"] == f"/runs/{EXTRACT_RUN}"
            assert nodes["load"][1]["href"] == f"/runs/{LOAD_LATEST}"
            assert nodes["report"][1]["href"] == "/jobs/report"
            assert "succeeded" in _box(page.text, "extract")
            assert "succeeded" in _box(page.text, "load")
            assert "no-run" in _box(page.text, "report")
            assert "load" in nodes["load"][2]
            assert LOAD_FIRST not in page.text

        h_.run(body)

    def test_edges_by_kind_and_cross_date(self, api: MakeApi) -> None:
        """Edges carry their ``on`` class; a run-date offset edge is ``cross-date`` (dashed)."""
        h_ = _graph(api)

        async def body() -> None:
            """Read the edges' classes."""
            edges = _edges((await _get(h_, f"/graph?run_date={OLD}")).text)
            assert set(edges) == {
                ("extract", "load"),
                ("load", "report"),
                ("report", "report"),
                ("extract", "audit"),
            }
            assert {"edge", "on-success"} <= edges["extract", "load"]
            assert "cross-date" not in edges["extract", "load"]
            assert "on-failure" in edges["load", "report"]
            assert "on-done" in edges["extract", "audit"]
            assert {"cross-date", "on-success"} <= edges["report", "report"]

        h_.run(body)

    def test_tag_filter(self, api: MakeApi) -> None:
        """``?tag=`` keeps that tag's jobs and only the edges between them."""
        h_ = _graph(api)

        async def body() -> None:
            """Filter by finance."""
            text = (await _get(h_, f"/graph?run_date={OLD}&tag=finance")).text
            assert set(_nodes(text)) == {"extract", "load"}
            assert set(_edges(text)) == {("extract", "load")}
            empty = (await _get(h_, f"/graph?run_date={OLD}&tag=nothing")).text
            assert not _nodes(empty)
            assert "No jobs" in empty

        h_.run(body)

    def test_focus_highlights_up_and_downstream(self, api: MakeApi) -> None:
        """``?focus=`` marks the job, lights its transitive neighbours and dims the rest."""
        h_ = _graph(api)

        async def body() -> None:
            """Focus on load, then on a job that is not there."""
            text = (await _get(h_, f"/graph?run_date={OLD}&focus=load")).text
            nodes = _nodes(text)
            assert "focus" in _classes(nodes["load"])
            for name in ("extract", "report"):
                assert "related" in _classes(nodes[name]), name
            for name in ("audit", "lonely"):
                assert "dimmed" in _classes(nodes[name]), name
            edges = _edges(text)
            assert "related" in edges["extract", "load"]
            assert "related" in edges["load", "report"]
            assert "dimmed" in edges["extract", "audit"]
            unknown = await _get(h_, f"/graph?run_date={OLD}&focus=ghost")
            assert unknown.status == OK
            assert "ghost" in unknown.text
            assert not any("dimmed" in _classes(n) for n in _nodes(unknown.text).values())

        h_.run(body)

    def test_dates(self, api: MakeApi) -> None:
        """No or an empty date means today there; a bad one is a 400 page."""
        h_ = _graph(api)
        today = START.astimezone(h_.config.settings.timezone).date()

        async def body() -> None:
            """Open the graph with no, an empty and a bad date."""
            for path in ("/graph", "/graph?run_date="):
                text = (await _get(h_, path)).text
                (picker,) = find(text, "input", name="run_date")
                assert picker[1]["value"] == today.isoformat(), path
            (back,) = find(text, "a", rel="prev")
            assert back[1]["href"] == f"/graph?run_date={today - timedelta(days=1)}"
            bad = await _get(h_, "/graph?run_date=nope")
            assert bad.status == BAD_REQUEST
            assert "run_date" in bad.text
            assert "<html" in bad.text

        h_.run(body)
