"""Draw the job page and the run page.

The **job page** is a passport: who the job is (its definition), when it
travels next, and the stamps of its last trips (a table of runs and a
squiggle of how long they took). The **run page** is one trip's logbook:
the summary, every attempt, every event, any alarms raised, and a live
window onto what the job is printing right now.

Everything here only draws; ``ui.detail`` reads the data. Every value
goes through ``ui.html``, so it is escaped, and the log lines never pass
through HTML at all (app.js puts them in with ``textContent``).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pynet.messaging import JsonValue

from pyscheduler.api.serialize import duration_text, instant
from pyscheduler.engine.model import Attempt, Run
from pyscheduler.engine.remote import remote_agent
from pyscheduler.ui.board_view import (
    DASH,
    BoardView,
    action_buttons,
    runs_table,
)
from pyscheduler.ui.html import Safe, h, join
from pyscheduler.ui.sparkline import sparkline

__all__ = [
    "RunPage",
    "job_body",
    "message_line",
    "run_body",
]

MESSAGE_ID = "board-message"
"""The id of the line app.js shows action errors in (the same on every page with buttons)."""

type JsonRow = Mapping[str, JsonValue]


@dataclass(frozen=True)
class RunPage:
    """Everything the run page draws besides the view: what was read for one run."""

    run: Run
    attempts: Sequence[Attempt]
    events: Sequence[JsonRow]
    alerts: Sequence[JsonRow] | None
    definition: JsonRow | None
    source: str | None
    source_note: str


def message_line() -> Safe:
    """Return the empty, hidden line action errors are shown in."""
    return h("p", {"id": MESSAGE_ID, "class": "error", "role": "status", "hidden": True})


def stamp(zone: ZoneInfo, value: datetime | None) -> Safe:
    """Return a local ``date time`` (the UTC instant as its title), or a dash."""
    if value is None:
        return h("span", None, DASH)
    local = value.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    return h("time", {"datetime": instant(value) or "", "title": instant(value)}, local)


def _cell(value: JsonValue) -> Safe:
    """Return a table cell for a JSON value: plain text, or its JSON in a ``<code>``."""
    if value is None or value == "":
        return h("td", None, DASH)
    if isinstance(value, str):
        return h("td", None, value)
    if isinstance(value, bool):
        return h("td", None, "yes" if value else "no")
    if isinstance(value, int | float):
        return h("td", None, str(value))
    return h("td", None, h("code", None, json.dumps(value, ensure_ascii=False)))


def definition_table(definition: JsonRow) -> Safe:
    """Return the job definition as a two-column table (name, value)."""
    return h(
        "table",
        {"class": "definition"},
        h(
            "tbody",
            None,
            *(
                h("tr", None, h("th", {"scope": "row"}, key), _cell(value))
                for key, value in definition.items()
            ),
        ),
    )


def _firings(view: BoardView, firings: Sequence[datetime]) -> Safe:
    """Return the list of the next firings (or a note that there are none)."""
    if not firings:
        return h("p", {"class": "empty"}, "Nothing is scheduled.")
    return h("ol", {"id": "firings"}, *(h("li", None, stamp(view.zone, f)) for f in firings))


def _trigger_forms(name: str) -> Safe:
    """Return the trigger and backfill controls (operators only); app.js posts them to the API."""
    return h(
        "section",
        {"id": "job-actions", "class": "job-actions", "data-job": name},
        h(
            "div",
            None,
            h("input", {"type": "date", "id": "trigger-date", "aria-label": "run date"}),
            h("button", {"type": "button", "data-job-action": "trigger"}, "Trigger now"),
        ),
        h(
            "div",
            None,
            h("input", {"type": "date", "id": "backfill-from", "aria-label": "backfill from"}),
            h("input", {"type": "date", "id": "backfill-to", "aria-label": "backfill to"}),
            h("button", {"type": "button", "data-job-action": "backfill"}, "Backfill"),
        ),
    )


def _took(view: BoardView, run: Run) -> timedelta | None:
    """Return how long the latest attempt of a succeeded ``run`` took, or ``None``."""
    attempt = view.latest.get(run.run_id)
    if attempt is None or attempt.ended_at is None or run.state.value != "succeeded":
        return None
    return attempt.ended_at - attempt.started_at


def job_body(
    view: BoardView,
    *,
    definition: JsonRow,
    firings: Sequence[datetime],
    runs: Sequence[Run],
) -> Safe:
    """Return the job page's body.

    ``runs`` is newest first; the sparkline draws the succeeded ones oldest first.
    """
    name = str(definition["name"])
    tooks = [t for t in (_took(view, run) for run in reversed(runs)) if t is not None]
    seconds = [t.total_seconds() for t in tooks]
    latest = f"latest {duration_text(tooks[-1])}" if tooks else "no succeeded runs yet"
    return join(
        h("h1", None, name),
        h("p", {"class": "lede"}, str(definition.get("description") or "")),
        message_line(),
        _trigger_forms(name) if view.operator else None,
        h("h2", None, "Next firings"),
        _firings(view, firings),
        h("h2", None, "Recent durations"),
        h("p", {"class": "spark"}, sparkline(seconds), " ", h("small", None, latest)),
        h("h2", None, "History"),
        runs_table(view, runs, {"id": "history"}),
        h("p", {"class": "empty", "hidden": bool(runs)}, "No runs yet."),
        h("h2", None, "Definition"),
        definition_table(definition),
    )


def _summary(view: BoardView, run: Run, reason: str) -> Safe:
    """Return the run's summary as a definition list."""
    job = h("a", {"href": f"/jobs/{quote(run.job, safe='')}"}, run.job)
    pairs: list[tuple[str, Safe]] = [
        ("Job", job),
        ("Run date", h("span", None, run.run_date.isoformat())),
        ("State", h("span", {"class": f"badge {run.state.value}"}, run.state.value)),
        ("Reason", h("span", None, reason or DASH)),
        ("Trigger", h("span", None, run.trigger.value)),
        ("Scheduled", stamp(view.zone, run.scheduled_at)),
        ("Attempts", h("span", None, str(run.attempt))),
        ("Reruns", h("span", None, str(run.rerun))),
        ("Config version", h("span", None, str(run.config_version))),
        ("Created", stamp(view.zone, run.created_at)),
        ("Updated", stamp(view.zone, run.updated_at)),
    ]
    return h(
        "dl", {"class": "summary"}, *(join(h("dt", None, k), h("dd", None, v)) for k, v in pairs)
    )


def _where(attempt: Attempt) -> Safe:
    """Return the cell saying where an attempt ran: its PID here, or the agent's name."""
    agent = remote_agent(attempt)
    if agent is not None:
        return h("td", None, h("a", {"href": "/agents", "class": "agent-name"}, agent))
    return h("td", None, DASH if attempt.pid is None else str(attempt.pid))


def _attempts(view: BoardView, attempts: Sequence[Attempt], reasons: Sequence[str]) -> Safe:
    """Return the attempts table."""
    heads = ("#", "Started", "Ended", "Took", "Exit", "Signal", "Outcome", "PID / agent", "Reason")
    rows = [
        h(
            "tr",
            {"data-attempt": str(a.attempt)},
            h("td", None, str(a.attempt)),
            h("td", None, stamp(view.zone, a.started_at)),
            h("td", None, stamp(view.zone, a.ended_at)),
            h(
                "td",
                None,
                DASH if a.ended_at is None else duration_text(a.ended_at - a.started_at),
            ),
            h("td", None, DASH if a.exit_code is None else str(a.exit_code)),
            h("td", None, DASH if a.signal is None else str(a.signal)),
            h("td", None, a.outcome.value if a.outcome else DASH),
            _where(a),
            h("td", None, reason or DASH),
        )
        for a, reason in zip(attempts, reasons, strict=True)
    ]
    return h(
        "table",
        {"id": "attempts"},
        h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, name) for name in heads))),
        h("tbody", None, *rows),
    )


def _events(events: Sequence[JsonRow]) -> Safe:
    """Return the events table (``at`` and ``detail`` come redacted from the caller)."""
    heads = ("At", "Actor", "Action", "Detail")
    return h(
        "table",
        {"id": "events"},
        h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, name) for name in heads))),
        h(
            "tbody",
            None,
            *(
                h(
                    "tr",
                    None,
                    _cell(e["at"]),
                    _cell(e["actor"]),
                    _cell(e["action"]),
                    _cell(e["detail"] if e["detail"] != {} else None),
                )
                for e in events
            ),
        ),
    )


def _delivery(row: JsonValue) -> Safe:
    """Return one delivery as a list item."""
    if not isinstance(row, dict):
        return h("li", None, DASH)
    error = row.get("last_error")
    text = f"{row.get('notifier')}: {row.get('status')} ({row.get('attempts')} tries)"
    return h("li", None, text if not error else f"{text} - {error}")


def _deliveries(alert: JsonRow) -> list[JsonValue]:
    """Return an alert's deliveries (as JSON rows)."""
    found = alert.get("deliveries")
    return found if isinstance(found, list) else []


def _alerts(alerts: Sequence[JsonRow]) -> Safe:
    """Return the alerts table with each alert's deliveries."""
    heads = ("Raised", "Kind", "Criticality", "Message", "Deliveries")
    return h(
        "table",
        {"id": "alerts"},
        h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, name) for name in heads))),
        h(
            "tbody",
            None,
            *(
                h(
                    "tr",
                    {"data-alert": str(a.get("alert_id"))},
                    _cell(a.get("raised_at")),
                    _cell(a.get("kind")),
                    _cell(a.get("criticality")),
                    _cell(a.get("message")),
                    h(
                        "td",
                        None,
                        h(
                            "ul",
                            None,
                            *(_delivery(d) for d in _deliveries(a)),
                        ),
                    ),
                )
                for a in alerts
            ),
        ),
    )


def _definition_section(page: RunPage) -> Safe:
    """Return the definition the run used: the table, or the old version's source text."""
    if page.definition is not None:
        return join(h("h2", None, "Definition"), definition_table(page.definition))
    version = page.run.config_version
    link = h("a", {"href": f"/admin/configs/{version}"}, f"config version {version}")
    return join(
        h("h2", None, "Definition"),
        h("p", {"id": "definition-note"}, page.source_note, " See ", link, "."),
        h("pre", {"id": "definition-source", "class": "source"}, page.source)
        if page.source is not None
        else None,
    )


def log_viewer(run: Run, attempts: Sequence[Attempt]) -> Safe:
    """Return the log viewer: stream tabs, an attempt picker and the text area app.js fills."""
    if not attempts:
        return h("p", {"class": "empty"}, "No attempt has started, so there is no log yet.")
    latest = attempts[-1].attempt
    return h(
        "section",
        {"id": "log", "class": "log", "data-run": run.run_id, "data-latest": str(latest)},
        h(
            "div",
            {"class": "log-controls"},
            h(
                "button",
                {"type": "button", "data-stream": "stdout", "aria-pressed": "true"},
                "stdout",
            ),
            h(
                "button",
                {"type": "button", "data-stream": "stderr", "aria-pressed": "false"},
                "stderr",
            ),
            h(
                "select",
                {"id": "log-attempt", "aria-label": "attempt"},
                *(
                    h(
                        "option",
                        {
                            "value": str(a.attempt),
                            "selected": a.attempt == latest,
                            "data-merged": "1" if a.stderr_path == a.stdout_path else "0",
                        },
                        f"attempt {a.attempt}",
                    )
                    for a in attempts
                ),
            ),
        ),
        h("p", {"id": "log-status", "role": "status"}),
        h("pre", {"id": "log-text", "class": "log-text"}),
    )


def run_body(view: BoardView, page: RunPage, reasons: Sequence[str], reason: str) -> Safe:
    """Return the run page's body.

    ``reasons`` are the attempts' reasons and ``reason`` the run's, already redacted.
    """
    run = page.run
    return h(
        "article",
        {"id": "run-page", "data-run-id": run.run_id, "data-attempts": str(run.attempt)},
        h("h1", None, run.run_id),
        message_line(),
        h(
            "p",
            {"id": "run-actions", "class": "actions"},
            *(action_buttons(run) if view.operator else []),
        ),
        _summary(view, run, reason),
        h("h2", None, "Log"),
        log_viewer(run, page.attempts),
        _definition_section(page),
        h("h2", None, "Attempts"),
        _attempts(view, page.attempts, reasons),
        h("h2", None, "Events"),
        _events(page.events),
        h("h2", None, "Alerts") if page.alerts is not None else None,
        _alerts(page.alerts) if page.alerts is not None else None,
    )
