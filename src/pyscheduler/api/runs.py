"""The run endpoints: list runs, show one, read its log, and act on it.

Reads show only the **published** view of the run book (states already
safe in the store) plus, for run dates older than the in-memory window,
what the store holds. Store reads and log files are read on a helper
thread, so the event loop keeps serving everyone else.
"""

import os
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest

from pyscheduler.api.serialize import attempt_json, event_json, run_json
from pyscheduler.api.support import (
    BAD_REQUEST,
    ApiError,
    actor_of,
    guarded,
    json_body,
    not_found,
    query_date,
    query_int,
    query_name,
)
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Event, Run, RunState

__all__ = ["add_run_routes", "find_run", "merged_runs", "read_tail"]

type JsonObject = dict[str, JsonValue]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_TAIL = 200
MAX_TAIL = 5000
MAX_TAIL_BYTES = 8 * 1024 * 1024
"""The most bytes read from the end of a log, however long its lines."""

BLOCK = 64 * 1024
"""How many bytes the tail reader steps back at a time."""

STREAMS = ("stdout", "stderr")
MAX_ATTEMPT = 10**9
"""The largest attempt number a log request may name."""


async def merged_runs(
    engine: Engine,
    *,
    run_date: date | None = None,
    job: str | None = None,
    state: RunState | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[Run]:
    """Return matching runs, newest ``scheduled_at`` first, at most ``limit``.

    The published view answers for runs in memory. Unless ``run_date`` is
    inside the in-memory window, the store is asked too (on a helper
    thread); a stored run that has a published copy is left out, so each
    run shows once, as published.
    """
    book = engine.book
    found = book.published_runs(run_date=run_date, job=job, state=state)
    if run_date is None or run_date < engine.memory_start():
        store = engine.store
        stored = await run_in_thread(
            lambda: store.query_runs(run_date=run_date, job=job, state=state, limit=limit)
        )
        shown = {run.run_id for run in found}
        found += [run for run in stored if run.run_id not in shown]
        found.sort(key=lambda run: run.run_id)
        found.sort(key=lambda run: run.scheduled_at, reverse=True)
    return found[:limit]


async def find_run(engine: Engine, run_id: str) -> Run:
    """Return the published copy of ``run_id``, or the stored one.

    Raises:
        ApiError: 404 if there is no such run.

    """
    run = engine.book.published(run_id)
    if run is None:
        run = await run_in_thread(engine.store.get_run, run_id)
    if run is None:
        msg = f"unknown run {run_id!r}"
        raise not_found(msg)
    return run


def read_tail(path: Path, count: int) -> tuple[list[str], bool] | None:
    """Return the last ``count`` lines of ``path`` and whether earlier text was left out.

    The file is read backwards in ``BLOCK``-sized steps, never more than
    ``MAX_TAIL_BYTES``, so a huge log costs no more than its tail. Bytes
    that are not UTF-8 become U+FFFD. Returns ``None`` if the file does
    not exist.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    with handle:
        pos = handle.seek(0, os.SEEK_END)
        data = b""
        while pos > 0 and len(data) < MAX_TAIL_BYTES:
            body = data[:-1] if data.endswith(b"\n") else data
            if body.count(b"\n") >= count:
                break
            step = min(BLOCK, pos, MAX_TAIL_BYTES - len(data))
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    if data.endswith(b"\n"):
        data = data[:-1]
    parts = data.split(b"\n") if data else []
    truncated = pos > 0
    if truncated and parts:
        parts = parts[1:]  # it may start part-way through a line
    if len(parts) > count:
        parts = parts[-count:]
        truncated = True
    lines = [part.decode("utf-8", errors="replace").removesuffix("\r") for part in parts]
    return lines, truncated


def _pick_attempt(attempts: list[Attempt], number: int | None) -> Attempt:
    """Return attempt ``number``, or the latest one.

    Raises:
        ApiError: 404 if the run has no such attempt (or none yet).

    """
    if not attempts:
        msg = "no log yet"
        raise not_found(msg)
    if number is None:
        return attempts[-1]
    for attempt in attempts:
        if attempt.attempt == number:
            return attempt
    msg = f"run has no attempt {number}"
    raise not_found(msg)


type _Action = Callable[[Engine, str, str, dict[str, JsonValue]], Awaitable[JsonObject]]


async def _rerun(engine: Engine, run_id: str, actor: str, body: dict[str, JsonValue]) -> JsonObject:
    """Rerun a final run, and its final downstream runs if asked."""
    downstream = body.get("downstream", False)
    if not isinstance(downstream, bool):
        raise ApiError(BAD_REQUEST, "downstream must be true or false")
    runs = await engine.rerun(run_id, downstream=downstream, actor=actor)
    return {"runs": [run_json(run) for run in runs]}


def _single(name: str) -> _Action:
    """Return the action that calls the engine method ``name`` and answers ``{"run": ...}``."""

    async def act(
        engine: Engine, run_id: str, actor: str, body: dict[str, JsonValue]
    ) -> JsonObject:
        """Call the engine method on the run."""
        del body
        method: Callable[..., Awaitable[Run]] = getattr(engine, name)
        return {"run": run_json(await method(run_id, actor=actor))}

    return act


ACTIONS: dict[str, tuple[_Action, tuple[str, ...]]] = {
    "rerun": (_rerun, ("downstream",)),
    "hold": (_single("hold"), ()),
    "release": (_single("release"), ()),
    "kill": (_single("kill"), ()),
    "force-start": (_single("force_start"), ()),
    "skip": (_single("skip"), ()),
    "mark-success": (_single("mark_success"), ()),
    "mark-failed": (_single("mark_failed"), ()),
}
"""Each run action's handler and the body fields it accepts."""


def add_run_routes(app: App, engine: Engine) -> None:
    """Register the ``/api/runs`` endpoints on ``app``."""

    @app.get("/api/runs")
    @guarded
    async def list_runs(request: HttpRequest) -> JsonObject:
        """List runs, filtered by ``run_date``, ``job`` and ``state``, with their latest attempt."""
        state_text = request.query.get("state")
        try:
            state = None if state_text is None else RunState(state_text)
        except ValueError:
            names = ", ".join(s.value for s in RunState)
            raise ApiError(BAD_REQUEST, f"state must be one of {names}") from None
        runs = await merged_runs(
            engine,
            run_date=query_date(request, "run_date"),
            job=query_name(request, "job"),
            state=state,
            limit=query_int(request, "limit", default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT),
        )
        started = [run.run_id for run in runs if run.attempt]
        latest = await run_in_thread(engine.store.latest_attempts, started)
        return {"runs": [run_json(run, latest.get(run.run_id), with_attempt=True) for run in runs]}

    @app.get("/api/runs/<run_id>")
    @guarded
    async def show_run(request: HttpRequest, run_id: str) -> JsonObject:
        """Show a run with its attempts and events."""
        del request
        run = await find_run(engine, run_id)
        store = engine.store

        def read() -> tuple[list[Attempt], list[Event]]:
            """Read the run's attempts and events from the store."""
            return store.attempts_for(run_id), store.events_for(run_id)

        attempts, events = await run_in_thread(read)
        return {
            "run": run_json(run, attempts[-1] if attempts else None),
            "attempts": [attempt_json(a) for a in attempts],
            "events": [event_json(e) for e in events],
        }

    @app.get("/api/runs/<run_id>/log")
    @guarded
    async def show_log(request: HttpRequest, run_id: str) -> JsonObject:
        """Return the last ``tail`` lines of an attempt's stdout or stderr."""
        stream = request.query.get("stream", "stdout")
        if stream not in STREAMS:
            raise ApiError(BAD_REQUEST, "stream must be stdout or stderr")
        tail = query_int(request, "tail", default=DEFAULT_TAIL, low=1, high=MAX_TAIL)
        number = query_int(request, "attempt", default=0, low=1, high=MAX_ATTEMPT) or None
        await find_run(engine, run_id)
        attempts = await run_in_thread(engine.store.attempts_for, run_id)
        attempt = _pick_attempt(attempts, number)
        if stream == "stderr" and attempt.stderr_path == attempt.stdout_path:
            msg = "stderr is merged into stdout: ask for stream=stdout"
            raise not_found(msg)
        path = attempt.stdout_path if stream == "stdout" else attempt.stderr_path
        found = await run_in_thread(read_tail, Path(path), tail) if path else None
        if found is None:
            msg = "no log yet"
            raise not_found(msg)
        lines, truncated = found
        return {"lines": list[JsonValue](lines), "path": path, "truncated": truncated}

    @app.post("/api/runs/<run_id>/<action>")
    @guarded
    async def act_on_run(request: HttpRequest, run_id: str, action: str) -> JsonObject:
        """Run one operator action on a run."""
        entry = ACTIONS.get(action)
        if entry is None:
            msg = f"unknown run action {action!r}"
            raise not_found(msg)
        handler, fields = entry
        body = dict(json_body(request, fields))
        return await handler(engine, run_id, actor_of(request), body)
