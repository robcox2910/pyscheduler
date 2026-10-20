"""The live log tail: watch an attempt's output grow, line by line.

Like reading over someone's shoulder while they write a letter: you
start with the last page they wrote, then every second you glance at
the pen and read the new words -- but only once a line is finished.
When they sign the letter (the attempt ends) you take one last look and
say so (``end``). If they tear the pages out and start again (the log
file was rotated and is now shorter), you say ``reset`` and read from
the top.

``GET /events/log/<run_id>?stream=stdout|stderr&attempt=`` sends:

- ``lines`` -- a JSON list of strings: first the last ``DEFAULT_TAIL``
  lines, then each new batch of whole lines;
- ``reset`` -- the file shrank, so the lines that follow start from its top;
- ``end`` -- the attempt has ended and everything it wrote has been sent;
  or, with ``{"message": ...}``, the log can no longer be read ("the agent
  is offline", "the agent did not answer").

The log is read through a ``LogSource`` every ``LOG_POLL`` seconds, at most
``LOG_CHUNK`` bytes at a time: a file on a helper thread, or, for an
attempt that ran on an agent, a request to that agent over the broker.
The path on the server never leaves it.

``LogTails.close_all`` ends every open tail at its next look, without
``end`` (a reload that swaps the users or the tokens does this): the
browser reconnects, is checked again at the door, and starts the tail
afresh.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pynet.loop import run_in_thread, sleep
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse
from pyweb.sse import EventStream, ServerEvent

from pyscheduler.agents.logsource import (
    LOG_CHUNK,
    LogSource,
    LogUnavailableError,
    log_source_for,
    read_from,
)
from pyscheduler.api.runs import DEFAULT_TAIL, log_path, log_query
from pyscheduler.api.support import ApiError, not_found
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt
from pyscheduler.ui.live import EMPTY, KEEPALIVE, LIVE_PATH

__all__ = ["LOG_CHUNK", "LOG_POLL", "LogTails", "TailTicket", "add_log_tail", "read_from"]

logger = logging.getLogger("pyscheduler.ui")

LOG_POLL = 1.0
"""Seconds between looks at the log file."""


@dataclass(eq=False)
class TailTicket:
    """One open tail's ticket: ``closed`` once the tail must stop."""

    closed: bool = False


class LogTails:
    """Keep a ticket for every open log tail, so they can all be stopped at once."""

    def __init__(self) -> None:
        """Start with no tails open."""
        self._open: set[TailTicket] = set()
        self._generation = 0

    @property
    def generation(self) -> int:
        """Return how many times ``close_all`` has run (a tail started earlier is stale)."""
        return self._generation

    def open(self) -> TailTicket:
        """Return a new open ticket for a tail that is starting."""
        ticket = TailTicket()
        self._open.add(ticket)
        return ticket

    def release(self, ticket: TailTicket) -> None:
        """Forget ``ticket`` (its tail has ended)."""
        self._open.discard(ticket)

    def close_all(self) -> None:
        """Close every open ticket; each tail stops at its next look."""
        self._generation += 1
        for ticket in self._open:
            ticket.closed = True
        self._open.clear()


def _split(data: bytes, *, flush: bool) -> tuple[list[str], bytes]:
    """Return the whole lines in ``data`` and the unfinished rest.

    With ``flush`` (the attempt is over), or when the rest is ``LOG_CHUNK``
    long, the rest is a line too.
    """
    *whole, rest = data.split(b"\n")
    if rest and (flush or len(rest) >= LOG_CHUNK):
        whole.append(rest)
        rest = b""
    return [line.decode("utf-8", errors="replace").removesuffix("\r") for line in whole], rest


def _lines(lines: list[str]) -> ServerEvent:
    """Return a ``lines`` event."""
    return ServerEvent(json.dumps(lines), event="lines")


_END = ServerEvent(EMPTY, event="end")


def _unavailable(error: LogUnavailableError) -> ServerEvent:
    """Return the ``end`` that stops the tail and says why (``{"message": ...}``)."""
    return ServerEvent(json.dumps({"message": str(error)}), event="end")


def _unreadable(path: str, error: OSError) -> ServerEvent:
    """Log (on the server only) why ``path`` cannot be read; return the ``end`` that stops the tail.

    Ending the stream (rather than failing it) keeps the browser from
    reconnecting over and over to a file it can never read.
    """
    logger.warning("log tail stopped: cannot read %s: %s", path, error.strerror or error)
    return _END


def _stopped(path: str, error: OSError | LogUnavailableError) -> ServerEvent:
    """Return the ``end`` for a log that cannot be read (see ``_unreadable``, ``_unavailable``)."""
    if isinstance(error, OSError):
        return _unreadable(path, error)
    return _unavailable(error)


async def _tail(
    engine: Engine,
    attempt: Attempt,
    source: LogSource,
    path: str,
    tails: LogTails,
    generation: int,
) -> AsyncIterator[ServerEvent]:
    """Yield the tail of ``source`` (the log at ``path``), then its new lines, then ``end`` once ``attempt`` is over.

    It holds a ticket from ``tails`` and stops early, without ``end``,
    once that ticket is closed. ``generation`` is ``tails.generation`` from
    when the caller passed the door: if ``close_all`` ran since, it ends at once.
    """
    if generation != tails.generation:
        return
    run_id, number = attempt.run_id, attempt.attempt
    ended = [attempt.ended_at is not None]

    def heard(row: Attempt) -> None:
        """Note the end of the watched attempt."""
        if row.run_id == run_id and row.attempt == number and row.ended_at is not None:
            ended[0] = True

    unwatch = engine.watch_attempts(heard)
    ticket = tails.open()
    try:
        store = engine.store

        def check() -> bool:
            """Check the attempt again (it may have ended meanwhile)."""
            rows = store.attempts_for(run_id)
            return any(r.attempt == number and r.ended_at is not None for r in rows)

        try:
            lines, offset = await source.first_look(DEFAULT_TAIL)
            over = await run_in_thread(check)
        except (OSError, LogUnavailableError) as exc:
            yield _stopped(path, exc)
            return
        ended[0] = ended[0] or over
        yield _lines(lines)
        rest = b""
        while True:
            await sleep(LOG_POLL)
            if ticket.closed:
                return
            final = ended[0]  # noted before this read, so the read sees all it wrote
            try:
                data, offset, shrank = await source.read(offset, LOG_CHUNK)
            except (OSError, LogUnavailableError) as exc:
                yield _stopped(path, exc)
                return
            if shrank:
                rest = b""
                yield ServerEvent(EMPTY, event="reset")
            last = final and len(data) < LOG_CHUNK
            lines, rest = _split(rest + data, flush=last)
            if lines:
                yield _lines(lines)
            if last:
                yield _END
                return
    finally:
        tails.release(ticket)
        unwatch()


def add_log_tail(app: App, engine: Engine, tails: LogTails | None = None) -> None:
    """Add ``GET /events/log/<run_id>`` (any logged-in user, like ``/api/runs/<id>/log``).

    The open tails are kept in ``tails`` (a new ``LogTails`` by default).
    """
    held = tails if tails is not None else LogTails()

    @app.get(f"{LIVE_PATH}/log/<run_id>")
    async def log_tail(request: HttpRequest, run_id: str) -> HttpResponse | EventStream:
        """Stream an attempt's log; answer JSON 400 or 404 as the log API does."""
        generation = held.generation  # before any wait: a swap after the door must end this tail
        try:
            stream, number = log_query(request)
            attempt, path = await log_path(engine, run_id, stream, number)
            if not path:
                msg = "no log yet"
                raise not_found(msg)
        except ApiError as exc:
            return exc.response()
        source = log_source_for(engine, attempt, stream, chunk=LOG_CHUNK)
        return EventStream(
            _tail(engine, attempt, source, path, held, generation), keepalive=KEEPALIVE
        )

    del log_tail
