"""Draw the job board: its filters, the counts by state, and one row per run.

This is the **departures board** at a station. Each row is a train (a
run): where it goes (the job), its status light, when it was due, when
it left and arrived, and whether it is running slow compared with how
long the trip usually takes. Station staff (operators) also get the
buttons to hold or send off a train; travellers (viewers) only read.

Everything here only draws: ``ui.board`` reads the runs and hands over
a ``BoardView``. Every value goes through ``ui.html``, so it is escaped.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from pynet.messaging import JsonValue

from pyscheduler.api.serialize import duration_text, instant, redact
from pyscheduler.config.model import Criticality, JobDef
from pyscheduler.engine.model import Attempt, Run, RunState
from pyscheduler.schedule.forecast import median
from pyscheduler.ui.dates import in_range
from pyscheduler.ui.html import Safe, h, join

__all__ = [
    "ACTIONS",
    "COUNTS_ID",
    "DASH",
    "FLAG_HIGH",
    "FLAG_LOW",
    "BoardView",
    "Filters",
    "action_buttons",
    "board_body",
    "clock_cell",
    "counts_nav",
    "pinned",
    "row",
    "runs_table",
]

DASH = "\N{EN DASH}"
"""What an unknown value shows as."""

FLAG_HIGH = 150
FLAG_LOW = 50
"""A run is flagged when it took more than ``FLAG_HIGH`` % or less than ``FLAG_LOW`` % of typical."""

COUNTS_ID = "board-counts"
"""The id of the counts bar (app.js swaps it for ``/ui/fragments/board-counts``)."""

_WAITING = ("hold", "force-start", "skip")
ACTIONS: Mapping[RunState, tuple[str, ...]] = {
    RunState.SCHEDULED: _WAITING,
    RunState.WAITING: _WAITING,
    RunState.BLOCKED: _WAITING,
    RunState.QUEUED: _WAITING,
    RunState.RETRY_WAIT: _WAITING,
    RunState.HELD: ("release", "force-start", "skip"),
    RunState.RUNNING: ("kill",),
    RunState.SUCCEEDED: ("rerun", "mark-failed"),
    RunState.FAILED: ("rerun", "mark-success"),
    RunState.SKIPPED: ("rerun", "mark-success", "mark-failed"),
    RunState.CANCELLED: ("rerun", "mark-success", "mark-failed"),
}
"""The run actions offered for each state (the API still checks each one)."""

_LABELS = {
    "hold": "Hold",
    "release": "Release",
    "force-start": "Force start",
    "skip": "Skip",
    "kill": "Kill",
    "rerun": "Rerun",
    "mark-success": "Mark success",
    "mark-failed": "Mark failed",
}

_COLUMNS = (
    "Job", "State", "Criticality", "Scheduled", "Start", "End",
    "Duration", "vs typical", "Attempt", "Tags",
)  # fmt: skip
_PERCENT = 100


@dataclass(frozen=True)
class Filters:
    """What the board shows: one run date, narrowed by state, tag, criticality and a search."""

    day: date
    state: RunState | None = None
    tag: str | None = None
    criticality: Criticality | None = None
    q: str | None = None

    def pairs(self) -> list[tuple[str, str]]:
        """Return the filters as query pairs (the unset ones left out)."""
        found = [
            ("date", self.day.isoformat()),
            ("state", None if self.state is None else self.state.value),
            ("tag", self.tag),
            ("criticality", None if self.criticality is None else self.criticality.name.lower()),
            ("q", self.q),
        ]
        return [(name, value) for name, value in found if value]

    def link(self) -> str:
        """Return the board's address with these filters."""
        return "/?" + urlencode(self.pairs())

    def matches(self, run: Run, job: JobDef | None) -> bool:
        """Return whether ``run`` (of ``job``, ``None`` if it is gone) passes every filter."""
        if self.state is not None and run.state is not self.state:
            return False
        if self.tag is not None and (job is None or self.tag not in job.tags):
            return False
        if self.criticality is not None and (job is None or job.criticality != self.criticality):
            return False
        return self.q is None or self.q.casefold() in run.job.casefold()


@dataclass(frozen=True)
class BoardView:
    """Everything the board draws with besides the runs themselves."""

    filters: Filters
    zone: ZoneInfo
    now: datetime
    jobs: Mapping[str, JobDef]
    expansions: Mapping[str, str]
    latest: Mapping[str, Attempt]
    durations: Mapping[str, Sequence[timedelta]]
    operator: bool


def clock_cell(view: BoardView, column: str, value: datetime | None) -> Safe:
    """Return a time cell: ``HH:MM:SS`` in the scheduler's zone (the UTC instant as its title)."""
    if value is None:
        return h("td", {"data-col": column}, DASH)
    local = value.astimezone(view.zone).strftime("%H:%M:%S")
    return h("td", {"data-col": column, "title": instant(value)}, local)


def _took(view: BoardView, run: Run, attempt: Attempt | None) -> timedelta | None:
    """Return how long the latest attempt took (so far, while it runs), or ``None``."""
    if attempt is None:
        return None
    if attempt.ended_at is not None:
        return attempt.ended_at - attempt.started_at
    return view.now - attempt.started_at if run.state is RunState.RUNNING else None


def _typical(view: BoardView, run: Run, took: timedelta | None) -> Safe:
    """Return the "vs typical" cell: ``took`` as a percentage of the job's median."""
    usual = median(view.durations.get(run.job))
    if took is None or usual is None or usual <= timedelta(0):
        return h("td", {"data-col": "typical"}, DASH)
    percent = round(took / usual * _PERCENT)
    flagged = percent > FLAG_HIGH or percent < FLAG_LOW
    return h(
        "td",
        {
            "data-col": "typical",
            "class": "off-typical" if flagged else None,
            "title": f"typical {duration_text(usual)}",
        },
        f"{percent}%",
    )


def action_buttons(run: Run) -> list[Safe]:
    """Return one button per action the run's state allows (app.js posts them to the API)."""
    return [
        h(
            "button",
            {"type": "button", "data-action": action, "data-run": run.run_id},
            _LABELS[action],
        )
        for action in ACTIONS[run.state]
    ]


def _actions(run: Run) -> Safe:
    """Return the actions cell: one button per action the run's state allows."""
    return h("td", {"data-col": "actions", "class": "actions"}, *action_buttons(run))


def row(view: BoardView, run: Run) -> Safe:
    """Return the board's ``<tr>`` for ``run``."""
    job = view.jobs.get(run.job)
    attempt = view.latest.get(run.run_id)
    took = _took(view, run, attempt)
    reason: JsonValue = redact(run.reason, view.expansions) if run.reason else ""
    criticality = job.criticality.name.lower() if job is not None else DASH
    critical = job is not None and job.criticality is Criticality.CRITICAL
    tags = job.tags if job is not None else ()
    return h(
        "tr",
        {
            "data-run-id": run.run_id,
            "data-pin": "0" if pinned(run, job) else "1",
            "data-at": run.scheduled_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "data-job": run.job,
        },
        h("td", {"data-col": "job"}, h("a", {"href": f"/jobs/{quote(run.job, safe='')}"}, run.job)),
        h(
            "td",
            {"data-col": "state"},
            h(
                "span",
                {"class": f"badge {run.state.value}", "title": str(reason) or None},
                run.state.value,
            ),
        ),
        h(
            "td",
            {"data-col": "criticality", "class": "critical" if critical else None},
            criticality,
        ),
        clock_cell(view, "scheduled", run.scheduled_at),
        clock_cell(view, "start", None if attempt is None else attempt.started_at),
        clock_cell(view, "end", None if attempt is None else attempt.ended_at),
        h("td", {"data-col": "duration"}, DASH if took is None else duration_text(took)),
        _typical(view, run, took),
        h(
            "td",
            {"data-col": "attempt"},
            h("a", {"href": f"/runs/{quote(run.run_id, safe='')}"}, str(run.attempt)),
        ),
        h(
            "td",
            {"data-col": "tags"},
            *(
                h("a", {"href": replace(view.filters, tag=tag).link(), "class": "tag"}, tag)
                for tag in tags
            ),
        ),
        _actions(run) if view.operator else None,
    )


def pinned(run: Run, job: JobDef | None) -> bool:
    """Return whether ``run`` is a failure of a critical job (pinned to the top, P3-R10)."""
    return (
        run.state is RunState.FAILED and job is not None and job.criticality is Criticality.CRITICAL
    )


def counts_nav(filters: Filters, counts: Mapping[RunState, int]) -> Safe:
    """Return the counts bar: the whole day's runs by state, each a link that filters by it."""
    total = sum(counts.values())
    return h(
        "nav",
        {"id": COUNTS_ID, "class": "counts", "aria-label": "runs by state"},
        h(
            "a",
            {
                "href": replace(filters, state=None).link(),
                "aria-current": "true" if filters.state is None else None,
            },
            "all ",
            h("b", None, str(total)),
        ),
        *(
            h(
                "a",
                {
                    "href": replace(filters, state=state).link(),
                    "data-state": state.value,
                    "aria-current": "true" if filters.state is state else None,
                },
                h("span", {"class": f"badge {state.value}"}, state.value),
                " ",
                h("b", None, str(counts[state])),
            )
            for state in RunState
            if counts.get(state)
        ),
    )


def _picker(filters: Filters) -> Safe:
    """Return the GET form: previous/next day, the date, a search and a criticality."""
    day = filters.day
    before = day - timedelta(days=1)
    after = day + timedelta(days=1)
    hidden = [
        ("state", None if filters.state is None else filters.state.value),
        ("tag", filters.tag),
    ]
    chosen = filters.criticality
    return h(
        "form",
        {"method": "get", "action": "/", "class": "board-filters"},
        h("a", {"href": replace(filters, day=before).link(), "rel": "prev"}, "← Previous day")
        if in_range(before)
        else None,
        h(
            "input",
            {"type": "date", "name": "date", "value": day.isoformat(), "aria-label": "run date"},
        ),
        h("a", {"href": replace(filters, day=after).link(), "rel": "next"}, "Next day →")
        if in_range(after)
        else None,
        h(
            "input",
            {
                "type": "text",
                "name": "q",
                "value": filters.q or "",
                "placeholder": "job name",
                "aria-label": "search jobs",
            },
        ),
        h(
            "select",
            {"name": "criticality", "aria-label": "criticality"},
            h("option", {"value": ""}, "any criticality"),
            *(
                h("option", {"value": c.name.lower(), "selected": c is chosen}, c.name.lower())
                for c in Criticality
            ),
        ),
        *(h("input", {"type": "hidden", "name": n, "value": v}) for n, v in hidden if v),
        h("button", {"type": "submit"}, "Show"),
    )


def runs_table(
    view: BoardView, runs: Sequence[Run], attrs: Mapping[str, str | bool | None] | None = None
) -> Safe:
    """Return the table of ``runs``, one board row each (``attrs`` go on the ``<table>``)."""
    heads = [*_COLUMNS, "Actions"] if view.operator else list(_COLUMNS)
    return h(
        "table",
        {"class": "board", **(attrs or {})},
        h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, name) for name in heads))),
        h("tbody", None, *(row(view, run) for run in runs)),
    )


def board_body(view: BoardView, runs: Sequence[Run], counts: Mapping[RunState, int]) -> Safe:
    """Return the board page's body: the filters, the counts and the table of ``runs``."""
    return join(
        h("h1", None, f"Board · {view.filters.day.isoformat()}"),
        _picker(view.filters),
        counts_nav(view.filters, counts),
        h("p", {"id": "board-message", "class": "error", "role": "status", "hidden": True}),
        runs_table(view, runs, {"id": "board", "data-date": view.filters.day.isoformat()}),
        h("p", {"id": "board-empty", "class": "empty", "hidden": bool(runs)}, "No runs match."),
    )
