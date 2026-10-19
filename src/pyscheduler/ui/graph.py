"""The dependency graph at ``/graph``: who waits for whom, coloured by one day's runs.

This is the **family tree of the jobs**. Each job is a box, and an arrow
runs from a job to every job that waits for it, left to right. Each box
is coloured by how that job's latest run of the chosen run date went (or
left plain when it has no run), and clicking it opens that run, or the
job when there is none.

``?run_date=`` picks the day (today in the scheduler's time zone when it
is missing or empty), ``?tag=`` keeps only the jobs with that tag (and
the arrows between them), and ``?focus=<job>`` lights up everything that
job waits for and everything that waits for it, near or far, and dims
the rest. All of it is drawn on the server; no script is needed.

Arrows to an earlier run date (``run_date: -1``) are dashed: they never
decide the columns (see ``ui.graph_layout``), so a job waiting on its
own yesterday is just a little loop over its box.
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse

from pyscheduler.api.runs import merged_runs
from pyscheduler.api.support import ApiError
from pyscheduler.config.model import JobDef
from pyscheduler.config.redaction import redactor
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Run
from pyscheduler.ui.board import BOARD_LIMIT
from pyscheduler.ui.dates import in_range, page_day
from pyscheduler.ui.graph_layout import NODE_H, NODE_W, Edge, Layout, layout, node_corner
from pyscheduler.ui.html import Safe, h, join
from pyscheduler.ui.layout import page

__all__ = ["NAME_CHARS", "NO_RUN", "GraphQuery", "add_graph", "graph_edges", "reach"]

NO_RUN = "no-run"
"""The box class of a job with no run on the shown date."""

NAME_CHARS = 20
"""The most characters of a job name that fit in its box (longer names are cut, with a tip)."""

_TEXT_PAD = 8
DAY = timedelta(days=1)
_RADIUS = "6"
_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"


@dataclass(frozen=True)
class GraphQuery:
    """What the graph shows: one run date, maybe narrowed by tag and focused on a job."""

    day: date
    tag: str | None = None
    focus: str | None = None

    def link(self) -> str:
        """Return the graph's address with these choices (the unset ones left out)."""
        pairs = [("run_date", self.day.isoformat()), ("tag", self.tag), ("focus", self.focus)]
        return "/graph?" + urlencode([(name, value) for name, value in pairs if value])


def _query(engine: Engine, request: HttpRequest) -> GraphQuery:
    """Return the graph's choices from the query (the date defaults to today there).

    Raises:
        ApiError: 400 for a run date that does not parse.

    """
    query = request.query
    today = engine.clock.now().astimezone(engine.config.settings.timezone).date()
    day = page_day(request, "run_date", today)
    return GraphQuery(day, query.get("tag") or None, query.get("focus") or None)


def graph_edges(jobs: Mapping[str, JobDef], kept: Iterable[str]) -> list[Edge]:
    """Return the dependencies between the ``kept`` jobs, in a fixed order."""
    names = set(kept)
    edges = {
        Edge(up=dep.job, down=name, on=dep.on.value, offset=dep.run_date_offset)
        for name in names
        for dep in jobs[name].depends_on
        if dep.job in names
    }
    return sorted(edges, key=lambda e: (e.up, e.down, e.offset, e.on))


def reach(edges: Sequence[Edge], start: str) -> tuple[set[str], set[str]]:
    """Return everything ``start`` waits for and everything that waits for it, over every edge.

    Both sets follow edges of any run-date offset, as far as they go, and
    leave ``start`` itself out.
    """

    def walk(step: Mapping[str, list[str]]) -> set[str]:
        """Return the names reachable from ``start`` along ``step``."""
        seen: set[str] = set()
        todo = deque([start])
        while todo:
            for other in step.get(todo.popleft(), []):
                if other not in seen:
                    seen.add(other)
                    todo.append(other)
        seen.discard(start)
        return seen

    ups: dict[str, list[str]] = {}
    downs: dict[str, list[str]] = {}
    for edge in edges:
        ups.setdefault(edge.down, []).append(edge.up)
        downs.setdefault(edge.up, []).append(edge.down)
    return walk(ups), walk(downs)


def _latest(runs: Iterable[Run]) -> dict[str, Run]:
    """Return each job's newest run (by due time, then creation, then id)."""
    latest: dict[str, Run] = {}
    for run in runs:
        seen = latest.get(run.job)
        if seen is None or (run.scheduled_at, run.created_at, run.run_id) > (
            seen.scheduled_at,
            seen.created_at,
            seen.run_id,
        ):
            latest[run.job] = run
    return latest


@dataclass(frozen=True)
class _Marks:
    """How the focus marks the drawing: the focused job and its neighbours, if any."""

    focus: str | None
    ups: frozenset[str] = frozenset()
    downs: frozenset[str] = frozenset()

    def node(self, name: str) -> str | None:
        """Return the node's focus class (``None`` with no focus)."""
        if self.focus is None:
            return None
        if name == self.focus:
            return "focus"
        return "related" if name in self.ups or name in self.downs else "dimmed"

    def edge(self, edge: Edge) -> str | None:
        """Return the edge's focus class: ``related`` on a path through the focus."""
        if self.focus is None:
            return None
        above = edge.up in self.ups and (edge.down in self.ups or edge.down == self.focus)
        below = edge.down in self.downs and (edge.up in self.downs or edge.up == self.focus)
        return "related" if above or below else "dimmed"


def _edge(edge: Edge, d: str, marks: _Marks) -> Safe:
    """Return one edge's ``<path>`` with its kind classes and a tip."""
    classes = ["edge", f"on-{edge.on}"]
    if edge.offset:
        classes.append("cross-date")
    mark = marks.edge(edge)
    if mark:
        classes.append(mark)
    when = f", run date {edge.offset:+d}" if edge.offset else ""
    tip = f"{edge.down} waits for {edge.up} (on {edge.on}{when})"
    return h(
        "path",
        {"d": d, "class": " ".join(classes), "data-up": edge.up, "data-down": edge.down},
        h("title", None, tip),
    )


def _node(
    name: str,
    place: tuple[int, int],
    run: Run | None,
    marks: _Marks,
    about: str,
) -> Safe:
    """Return one job's box: a link (to its run, else the job) holding the rect and the name."""
    x, y = node_corner(place)
    state = NO_RUN if run is None else run.state.value
    href = f"/jobs/{quote(name, safe='')}" if run is None else f"/runs/{quote(run.run_id, safe='')}"
    label = name if len(name) <= NAME_CHARS else name[: NAME_CHARS - 1] + _ELLIPSIS
    tip = f"{name}: {state.replace('-', ' ')}" + (f" \N{EM DASH} {about}" if about else "")
    mark = marks.node(name)
    return h(
        "a",
        {"href": href, "class": f"node {mark}" if mark else "node", "data-job": name},
        h("title", None, tip),
        h(
            "rect",
            {
                "class": f"box {state}",
                "x": str(x),
                "y": str(y),
                "width": str(NODE_W),
                "height": str(NODE_H),
                "rx": _RADIUS,
            },
        ),
        h("text", {"x": str(x + _TEXT_PAD), "y": str(y + NODE_H // 2)}, label),
    )


def _drawing(
    shape: Layout,
    runs: Mapping[str, Run],
    marks: _Marks,
    about: Callable[[str], str],
) -> Safe:
    """Return the ``<svg>``: the edges first, then the boxes on top."""
    return h(
        "svg",
        {
            "class": "graph",
            "role": "img",
            "aria-label": "dependency graph",
            "width": str(shape.width),
            "height": str(shape.height),
            "viewBox": f"0 0 {shape.width} {shape.height}",
        },
        h("g", {"class": "edges"}, *(_edge(e, d, marks) for e, d in shape.paths.items())),
        h(
            "g",
            {"class": "nodes"},
            *(
                _node(name, place, runs.get(name), marks, about(name))
                for name, place in shape.positions.items()
            ),
        ),
    )


def _picker(choice: GraphQuery, names: Sequence[str]) -> Safe:
    """Return the GET form: previous/next day, the date, a tag and the job to focus on."""
    day = choice.day
    before = day - DAY
    after = day + DAY
    return h(
        "form",
        {"method": "get", "action": "/graph", "class": "board-filters"},
        h("a", {"href": replace(choice, day=before).link(), "rel": "prev"}, "← Previous day")
        if in_range(before)
        else None,
        h(
            "input",
            {
                "type": "date",
                "name": "run_date",
                "value": day.isoformat(),
                "aria-label": "run date",
            },
        ),
        h("a", {"href": replace(choice, day=after).link(), "rel": "next"}, "Next day →")
        if in_range(after)
        else None,
        h(
            "input",
            {
                "type": "text",
                "name": "tag",
                "value": choice.tag or "",
                "placeholder": "tag",
                "aria-label": "tag",
            },
        ),
        h(
            "select",
            {"name": "focus", "aria-label": "focus on a job"},
            h("option", {"value": ""}, "no focus"),
            *(h("option", {"value": n, "selected": n == choice.focus}, n) for n in names),
        ),
        h("button", {"type": "submit"}, "Show"),
    )


_LEGEND = (
    ("on-success", "on success"),
    ("on-failure", "on failure"),
    ("on-done", "on done"),
    ("cross-date", "earlier run date"),
)


def _legend() -> Safe:
    """Return the key to the edge styles."""
    return h(
        "ul",
        {"class": "graph-legend", "aria-label": "edge kinds"},
        *(h("li", {"class": kind}, label) for kind, label in _LEGEND),
    )


async def _body(engine: Engine, choice: GraphQuery) -> Safe:
    """Return the graph page's body for ``choice``."""
    config = engine.config
    jobs = config.jobs
    tag = choice.tag
    names = sorted(name for name, job in jobs.items() if tag is None or tag in job.tags)
    edges = graph_edges(jobs, names)
    note: Safe | None = None
    marks = _Marks(None)
    if choice.focus is not None:
        if choice.focus in names:
            ups, downs = reach(edges, choice.focus)
            marks = _Marks(choice.focus, frozenset(ups), frozenset(downs))
        else:
            text = f"No shown job is named {choice.focus}, so nothing is focused."
            note = h("p", {"class": "note", "role": "status"}, text)
    runs = _latest(await merged_runs(engine, run_date=choice.day, limit=BOARD_LIMIT))
    hide = redactor(config.expansions)
    drawing = (
        h(
            "div",
            {"class": "graph-wrap"},
            _drawing(layout(names, edges), runs, marks, lambda n: hide(jobs[n].description)),
        )
        if names
        else h("p", {"class": "empty"}, "No jobs match.")
    )
    return join(
        h("h1", None, f"Graph · {choice.day.isoformat()}"),
        _picker(choice, names),
        note,
        _legend(),
        drawing,
    )


def add_graph(app: App, engine: Engine) -> None:
    """Add the dependency graph (``/graph``) to ``app``."""

    @app.get("/graph")
    async def graph(request: HttpRequest) -> HttpResponse:
        """Draw the graph for the asked run date, tag and focus."""
        try:
            choice = _query(engine, request)
        except ApiError as exc:
            body = h("p", {"class": "error", "role": "alert"}, exc.message)
            response = page(request, title="Graph", body=body, active="graph")
            response.status = exc.status
            return response
        return page(request, title="Graph", body=await _body(engine, choice), active="graph")

    del graph
