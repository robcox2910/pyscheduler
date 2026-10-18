"""The job endpoints: list and show jobs, trigger and backfill them, and the forecast.

The config is read-only, so finding next firings and forecasts (which
may walk many days of calendars) happens on a helper thread with the
config in force when the request came in.
"""

from datetime import datetime, timedelta

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest

from pyscheduler.api.auth import allows
from pyscheduler.api.runs import merged_runs
from pyscheduler.api.serialize import (
    definition_json,
    forecast_json,
    instant,
    job_json,
    run_json,
)
from pyscheduler.api.support import (
    BAD_REQUEST,
    ApiError,
    actor_of,
    body_date,
    guarded,
    json_body,
    not_found,
    role_of,
)
from pyscheduler.config import Config, JobDef
from pyscheduler.engine.core import Engine
from pyscheduler.schedule.firings import next_firing
from pyscheduler.schedule.forecast import forecast

__all__ = ["add_job_routes", "next_firings"]

type JsonObject = dict[str, JsonValue]

NEXT_FIRINGS = 5
RECENT_RUNS = 20
DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=31)
OUT_OF_RANGE = "the forecast window is outside the dates the scheduler can handle"


def next_firings(config: Config, job: JobDef, after: datetime, count: int) -> list[datetime]:
    """Return up to ``count`` instants ``job`` fires at after ``after`` (none if disabled)."""
    if not job.enabled or not job.schedules:
        return []
    calendar = config.calendar_for(job)
    found: list[datetime] = []
    while len(found) < count:
        firing = next_firing(job, calendar, found[-1] if found else after)
        if firing is None:
            break
        found.append(firing.scheduled_at)
    return found


def _first(config: Config, job: JobDef, now: datetime) -> datetime | None:
    """Return the next instant ``job`` fires at, or ``None``."""
    upcoming = next_firings(config, job, now, 1)
    return upcoming[0] if upcoming else None


def _instant(request: HttpRequest, name: str, default: datetime) -> datetime:
    """Return the aware ISO 8601 query parameter ``name`` (``default`` when absent).

    Raises:
        ApiError: 400 if it is not an ISO instant with a time zone.

    """
    text = request.query.get(name)
    if text is None:
        return default
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ApiError(BAD_REQUEST, f"{name} must be an ISO 8601 instant") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiError(BAD_REQUEST, f"{name} needs a time zone, such as Z or +01:00")
    return value


def add_job_routes(app: App, engine: Engine) -> None:
    """Register the ``/api/jobs`` and ``/api/forecast`` endpoints on ``app``."""

    def job_named(name: str) -> JobDef:
        """Return the job ``name`` of the config in force.

        Raises:
            ApiError: 404 if there is no such job.

        """
        job = engine.config.jobs.get(name)
        if job is None:
            msg = f"unknown job {name!r}"
            raise not_found(msg)
        return job

    @app.get("/api/jobs")
    @guarded
    async def list_jobs(request: HttpRequest) -> JsonObject:
        """Summarise every job, with when it fires next."""
        del request
        config, now = engine.config, engine.clock.now()

        def build() -> list[JsonValue]:
            """Summarise the jobs in name order."""
            return [
                job_json(config, job, _first(config, job, now))
                for _, job in sorted(config.jobs.items())
            ]

        return {"jobs": await run_in_thread(build)}

    @app.get("/api/jobs/<name>")
    @guarded
    async def show_job(request: HttpRequest, name: str) -> JsonObject:
        """Show a job's summary, definition, next firings and recent runs."""
        config, now = engine.config, engine.clock.now()
        job = job_named(name)
        upcoming = await run_in_thread(next_firings, config, job, now, NEXT_FIRINGS)
        runs = await merged_runs(engine, job=name, limit=RECENT_RUNS)
        out = job_json(config, job, upcoming[0] if upcoming else None)
        show_alerts = allows(role_of(request), "operator")
        out["definition"] = definition_json(config, job, show_alerts=show_alerts)
        out["next_firings"] = [instant(f) for f in upcoming]
        out["recent_runs"] = [run_json(run) for run in runs]
        return out

    @app.post("/api/jobs/<name>/trigger")
    @guarded
    async def trigger(request: HttpRequest, name: str) -> JsonObject:
        """Create a manual run of a job, for today or the given ``run_date``."""
        body = json_body(request, ("run_date",))
        run_date = body_date(body, "run_date")
        run = await engine.trigger(name, run_date=run_date, actor=actor_of(request))
        return {"run": run_json(run)}

    @app.post("/api/jobs/<name>/backfill")
    @guarded
    async def backfill(request: HttpRequest, name: str) -> JsonObject:
        """Create runs of a job for every business day ``from`` .. ``to``."""
        body = json_body(request, ("from", "to"))
        start, end = body_date(body, "from"), body_date(body, "to")
        if start is None or end is None:
            raise ApiError(BAD_REQUEST, "backfill needs both from and to (YYYY-MM-DD)")
        runs = await engine.backfill(name, start=start, end=end, actor=actor_of(request))
        return {"runs": [run_json(run) for run in runs]}

    _add_forecast_route(app, engine)


def _add_forecast_route(app: App, engine: Engine) -> None:
    """Register ``/api/forecast`` on ``app``."""

    @app.get("/api/forecast")
    @guarded
    async def show_forecast(request: HttpRequest) -> JsonObject:
        """Predict the runs between ``from`` and ``to`` (default: the next 24 hours)."""
        start = _instant(request, "from", engine.clock.now())
        try:
            end = _instant(request, "to", start + DEFAULT_WINDOW)
        except OverflowError:
            raise ApiError(BAD_REQUEST, OUT_OF_RANGE) from None
        if end < start:
            raise ApiError(BAD_REQUEST, "to is before from")
        if end - start > MAX_WINDOW:
            raise ApiError(BAD_REQUEST, f"the window may be at most {MAX_WINDOW.days} days")
        config, store = engine.config, engine.store

        def build() -> list[JsonValue]:
            """Read past durations and forecast the window."""
            durations = {name: store.recent_durations(name) for name in config.jobs}
            entries = forecast(config, start, end, durations=durations)
            return [forecast_json(entry) for entry in entries]

        try:
            return {"entries": await run_in_thread(build)}
        except OverflowError:
            raise ApiError(BAD_REQUEST, OUT_OF_RANGE) from None
