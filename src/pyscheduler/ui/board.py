"""The job board at ``/``: one run date's runs, live.

The board is the station's departures board. ``/`` draws it for one
run date (today in the scheduler's time zone unless ``?date=`` says
otherwise), narrowed by ``?state=``, ``?tag=``, ``?criticality=`` and a
job-name search ``?q=``. The counts above the table always cover the
whole day, so you can see at a glance what the filter is hiding.

The board keeps itself up to date: app.js listens to the live feed and,
when a run of the shown date changes, fetches that run's row again from
``/ui/fragments/board-row/<run_id>`` (and the counts from
``/ui/fragments/board-counts``). The server draws the row, so the HTML is
built (and escaped) in one place only.

Typical durations come from one store read for every job on the page
(``Store.recent_durations_for``), on a helper thread, with the latest
attempts, so a busy day costs one trip to the store.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import timedelta

from pynet.loop import run_in_thread
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse, html

from pyscheduler.api.auth import allows
from pyscheduler.api.runs import find_run, merged_runs
from pyscheduler.api.support import BAD_REQUEST, ApiError, query_date, role_of
from pyscheduler.config.model import Criticality
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Run, RunState
from pyscheduler.ui.board_view import BoardView, Filters, board_body, counts_nav, pinned, row
from pyscheduler.ui.html import Safe, h
from pyscheduler.ui.layout import page, secure

__all__ = ["BOARD_LIMIT", "add_board"]

BOARD_LIMIT = 100_000
"""The most runs one board shows (far more than any real day)."""

ROW_FRAGMENT = "/ui/fragments/board-row/<run_id>"
COUNTS_FRAGMENT = "/ui/fragments/board-counts"
_NOT_FOUND = 404


def _filters(engine: Engine, request: HttpRequest) -> Filters:
    """Return the board's filters from the query (the date defaults to today there).

    Raises:
        ApiError: 400 for a date, state or criticality that does not parse.

    """
    zone = engine.config.settings.timezone
    day = query_date(request, "date") or engine.clock.now().astimezone(zone).date()
    query = request.query
    state_text = query.get("state") or None
    try:
        state = None if state_text is None else RunState(state_text)
    except ValueError:
        names = ", ".join(s.value for s in RunState)
        raise ApiError(BAD_REQUEST, f"state must be one of {names}") from None
    level = query.get("criticality") or None
    try:
        criticality = None if level is None else Criticality.from_text(level)
    except ValueError as exc:
        raise ApiError(BAD_REQUEST, str(exc)) from None
    return Filters(day, state, query.get("tag") or None, criticality, query.get("q") or None)


def _ordered(engine: Engine, runs: Sequence[Run]) -> list[Run]:
    """Return ``runs`` in board order: critical failures, then by due time and job (P3-R10)."""
    jobs = engine.config.jobs
    return sorted(
        runs,
        key=lambda r: (not pinned(r, jobs.get(r.job)), r.scheduled_at, r.job, r.run_id),
    )


async def _view(
    engine: Engine, request: HttpRequest, filters: Filters, runs: list[Run]
) -> BoardView:
    """Read the latest attempts and the typical durations of ``runs`` (one helper-thread trip)."""
    store = engine.store
    started = [run.run_id for run in runs if run.attempt]
    jobs = sorted({run.job for run in runs})

    def read() -> tuple[dict[str, Attempt], dict[str, list[timedelta]]]:
        """Read the attempts and durations from the store."""
        return store.latest_attempts(started), store.recent_durations_for(jobs)

    latest, durations = await run_in_thread(read)
    config = engine.config
    return BoardView(
        filters=filters,
        zone=config.settings.timezone,
        now=engine.clock.now(),
        jobs=config.jobs,
        expansions=config.expansions,
        latest=latest,
        durations=durations,
        operator=allows(role_of(request), "operator"),
    )


async def _day(engine: Engine, filters: Filters) -> tuple[list[Run], Counter[RunState]]:
    """Return the shown runs of the filters' day, in board order, and the whole day's counts."""
    runs = await merged_runs(engine, run_date=filters.day, limit=BOARD_LIMIT)
    counts = Counter(run.state for run in runs)
    jobs = engine.config.jobs
    shown = [run for run in runs if filters.matches(run, jobs.get(run.job))]
    return _ordered(engine, shown), counts


def _refused(request: HttpRequest, error: ApiError) -> HttpResponse:
    """Return the board page with the error message (status from ``error``)."""
    body = h("p", {"class": "error", "role": "alert"}, error.message)
    response = page(request, title="Board", body=body, active="board")
    response.status = error.status
    return response


def _fragment(text: Safe, status: int = 200) -> HttpResponse:
    """Return a bit of HTML (not a page) with the security headers."""
    return secure(html(text, status))


def add_board(app: App, engine: Engine) -> None:
    """Add the board (``/``) and its live fragments to ``app``."""

    @app.get("/")
    async def board(request: HttpRequest) -> HttpResponse:
        """Draw the board for the asked date and filters."""
        try:
            filters = _filters(engine, request)
        except ApiError as exc:
            return _refused(request, exc)
        shown, counts = await _day(engine, filters)
        view = await _view(engine, request, filters, shown)
        return page(request, title="Board", body=board_body(view, shown, counts), active="board")

    @app.get(ROW_FRAGMENT)
    async def board_row(request: HttpRequest, run_id: str) -> HttpResponse:
        """Draw one run's row; 404 if it is unknown, of another date or filtered out."""
        try:
            filters = _filters(engine, request)
            run = await find_run(engine, run_id)
        except ApiError as exc:
            return _fragment(h("p", None, exc.message), exc.status)
        if run.run_date != filters.day or not filters.matches(run, engine.config.jobs.get(run.job)):
            return _fragment(h("p", None, "not on this board"), _NOT_FOUND)
        view = await _view(engine, request, filters, [run])
        return _fragment(row(view, run))

    @app.get(COUNTS_FRAGMENT)
    async def board_counts(request: HttpRequest) -> HttpResponse:
        """Draw the counts bar for the asked date (its links keep the other filters)."""
        try:
            filters = _filters(engine, request)
        except ApiError as exc:
            return _fragment(h("p", None, exc.message), exc.status)
        runs = await merged_runs(engine, run_date=filters.day, limit=BOARD_LIMIT)
        return _fragment(counts_nav(filters, Counter(run.state for run in runs)))

    del board, board_row, board_counts
