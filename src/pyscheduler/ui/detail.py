"""The job page (``/jobs/<name>``) and the run page (``/runs/<run_id>``).

The job page answers "what is this job and how has it been doing?". The
run page answers "what happened on this one trip?", and shows the job's
output as it is written (app.js follows ``/events/log/<run_id>``).

A run remembers the config version it was made under. When that is still
the version in force, the page shows the job's current definition. When it
is older, the scheduler does not re-read the old files (they may need
secrets that are gone); it shows the saved text of the job's own file
instead, to operators, with a link to the config history.

Everything goes through ``redact`` like the API, alerts and the old source
text are for operators only, and every store read runs on a helper thread.
"""

import json
from pathlib import Path
from typing import cast

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse

from pyscheduler.api.alerts import alert_rows
from pyscheduler.api.auth import allows
from pyscheduler.api.jobs import next_firings
from pyscheduler.api.runs import find_run, merged_runs
from pyscheduler.api.serialize import definition_json, event_json, redact
from pyscheduler.api.support import ApiError, role_of
from pyscheduler.config.model import JobDef
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Run
from pyscheduler.ui.board import build_view
from pyscheduler.ui.board_view import Filters
from pyscheduler.ui.detail_view import JsonRow, RunPage, job_body, run_body
from pyscheduler.ui.html import h
from pyscheduler.ui.layout import page

__all__ = ["FIRINGS", "HISTORY", "add_detail"]

FIRINGS = 10
"""How many upcoming firings the job page lists."""

HISTORY = 30
"""How many recent runs the job page lists (and draws in its sparkline)."""

_WHERE = "the config in force"


def _refused(request: HttpRequest, error: ApiError, active: str) -> HttpResponse:
    """Return a page with the error message (status from ``error``)."""
    body = h("p", {"class": "error", "role": "alert"}, error.message)
    response = page(request, title="Not found", body=body, active=active)
    response.status = error.status
    return response


def _rows(values: list[JsonValue]) -> list[JsonRow]:
    """Return the JSON objects among ``values``."""
    return [v for v in values if isinstance(v, dict)]


def _old_source(
    engine: Engine, run: Run, job: JobDef | None, *, operator: bool
) -> tuple[str | None, str]:
    """Return the saved text of the job's own file at the run's config version, and a note.

    Reads the store: call it on a helper thread.
    """
    version = run.config_version
    base = f"This run used config version {version}, not the current {engine.config_version}."
    if not operator:
        return None, f"{base} Operators can see that version's source."
    record = engine.store.get_config(version)
    if record is None:
        return None, f"{base} That version is no longer stored."
    if job is None:
        return None, f"{base} The job is no longer in the current config, so its file is unknown."
    sources = cast("dict[str, JsonValue]", json.loads(record.sources_json))
    text = sources.get(job.source.file)
    if not isinstance(text, str):
        return None, f"{base} That version has no saved text for the job's file."
    shown = redact(text, engine.config.expansions)
    return str(shown), f"{base} Its file {Path(job.source.file).name} then read:"


def add_detail(app: App, engine: Engine) -> None:
    """Add ``/jobs/<name>`` and ``/runs/<run_id>`` to ``app``."""

    @app.get("/jobs/<name>")
    async def job_page(request: HttpRequest, name: str) -> HttpResponse:
        """Draw a job's definition, next firings, history and duration sparkline."""
        config = engine.config
        job = config.jobs.get(name)
        if job is None:
            return _refused(request, ApiError(404, f"unknown job {name!r}"), "board")
        operator = allows(role_of(request), "operator")
        firings = await run_in_thread(next_firings, config, job, engine.clock.now(), FIRINGS)
        runs = await merged_runs(engine, job=name, limit=HISTORY)
        today = engine.clock.now().astimezone(config.settings.timezone).date()
        view = await build_view(engine, request, Filters(today), runs)
        definition = redact(definition_json(config, job, show_alerts=operator), config.expansions)
        body = job_body(view, definition=cast(JsonRow, definition), firings=firings, runs=runs)
        return page(request, title=name, body=body, active="board")

    @app.get("/runs/<run_id>")
    async def run_page(request: HttpRequest, run_id: str) -> HttpResponse:
        """Draw a run's summary, definition, attempts, events, alerts and live log."""
        try:
            run = await find_run(engine, run_id)
        except ApiError as exc:
            return _refused(request, exc, "board")
        config, store = engine.config, engine.store
        operator = allows(role_of(request), "operator")
        job = config.jobs.get(run.job)
        current = run.config_version == engine.config_version

        def read() -> RunPage:
            """Read what the page shows from the store (one helper-thread trip)."""
            attempts = store.attempts_for(run_id)
            events: list[JsonValue] = [event_json(e) for e in store.events_for(run_id)]
            alerts = _rows(alert_rows(engine, store.alerts_for(run_id))) if operator else None
            definition: JsonRow | None = None
            source: str | None = None
            note = ""
            if current:
                if job is not None:
                    shown = definition_json(config, job, show_alerts=operator)
                    definition = cast(JsonRow, redact(shown, config.expansions))
                else:
                    note = f"The job is no longer defined in {_WHERE}."
            else:
                source, note = _old_source(engine, run, job, operator=operator)
            return RunPage(
                run, attempts, _rows(cast("list[JsonValue]", redact(events, config.expansions))),
                alerts, definition, source, note,
            )  # fmt: skip

        found = await run_in_thread(read)
        view = await build_view(engine, request, Filters(run.run_date), [run])
        expansions = config.expansions
        reasons = [str(redact(a.reason, expansions)) for a in found.attempts]
        reason = str(redact(run.reason, expansions))
        body = run_body(view, found, reasons, reason)
        return page(request, title=run_id, body=body, active="board")

    del job_page, run_page
