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
- ``end`` -- the attempt has ended and everything it wrote has been sent.

The file is read on a helper thread every ``LOG_POLL`` seconds, at most
``LOG_CHUNK`` bytes at a time. The path on the server never leaves it.
"""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from pynet.loop import run_in_thread, sleep
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse
from pyweb.sse import EventStream, ServerEvent

from pyscheduler.api.runs import DEFAULT_TAIL, log_path, log_query, read_tail
from pyscheduler.api.support import ApiError, not_found
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt
from pyscheduler.ui.live import EMPTY, KEEPALIVE, LIVE_PATH

__all__ = ["LOG_CHUNK", "LOG_POLL", "add_log_tail", "read_from"]

LOG_POLL = 1.0
"""Seconds between looks at the log file."""

LOG_CHUNK = 65536
"""The most bytes read from the log file per look (and the longest held-back line)."""


def read_from(path: Path, offset: int, limit: int) -> tuple[bytes, int, bool]:
    """Return up to ``limit`` bytes of ``path`` from ``offset``, the new offset, and if it shrank.

    A file shorter than ``offset`` was rotated: it is read from 0 instead
    and the flag is true. A missing file reads as nothing new.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return b"", offset, False
    with handle:
        size = handle.seek(0, os.SEEK_END)
        shrank = size < offset
        start = 0 if shrank else offset
        handle.seek(start)
        data = handle.read(limit)
    return data, start + len(data), shrank


def _first_look(path: Path, count: int) -> tuple[list[str], int]:
    """Return the last ``count`` whole lines of ``path`` and the offset just after them.

    An unfinished last line is left for the polls, so it is sent whole.
    (A last line longer than ``LOG_CHUNK`` is sent as it stands.)
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], 0
    back = min(size, LOG_CHUNK)
    data, _, _ = read_from(path, size - back, back)
    cut = data.rfind(b"\n")
    if cut >= 0:
        end = size - back + cut + 1
    elif back == size:
        end = 0  # the whole file is one unfinished line
    else:
        end = size
    found = read_tail(path, count, end=end) if end else None
    return (found[0] if found else []), end


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


async def _tail(engine: Engine, attempt: Attempt, path: Path) -> AsyncIterator[ServerEvent]:
    """Yield the tail of ``path``, then its new lines, then ``end`` once ``attempt`` is over."""
    run_id, number = attempt.run_id, attempt.attempt
    ended = [attempt.ended_at is not None]

    def heard(row: Attempt) -> None:
        """Note the end of the watched attempt."""
        if row.run_id == run_id and row.attempt == number and row.ended_at is not None:
            ended[0] = True

    unwatch = engine.watch_attempts(heard)
    try:
        store = engine.store

        def look() -> tuple[list[str], int, bool]:
            """Read the first tail, and check the attempt again (it may have ended meanwhile)."""
            lines, offset = _first_look(path, DEFAULT_TAIL)
            rows = store.attempts_for(run_id)
            over = any(r.attempt == number and r.ended_at is not None for r in rows)
            return lines, offset, over

        lines, offset, over = await run_in_thread(look)
        ended[0] = ended[0] or over
        yield _lines(lines)
        rest = b""
        while True:
            await sleep(LOG_POLL)
            final = ended[0]  # noted before this read, so the read sees all it wrote
            data, offset, shrank = await run_in_thread(read_from, path, offset, LOG_CHUNK)
            if shrank:
                rest = b""
                yield ServerEvent(EMPTY, event="reset")
            last = final and len(data) < LOG_CHUNK
            lines, rest = _split(rest + data, flush=last)
            if lines:
                yield _lines(lines)
            if last:
                yield ServerEvent(EMPTY, event="end")
                return
    finally:
        unwatch()


def add_log_tail(app: App, engine: Engine) -> None:
    """Add ``GET /events/log/<run_id>`` (any logged-in user, like ``/api/runs/<id>/log``)."""

    @app.get(f"{LIVE_PATH}/log/<run_id>")
    async def log_tail(request: HttpRequest, run_id: str) -> HttpResponse | EventStream:
        """Stream an attempt's log; answer JSON 400 or 404 as the log API does."""
        try:
            stream, number = log_query(request)
            attempt, path = await log_path(engine, run_id, stream, number)
            if not path:
                msg = "no log yet"
                raise not_found(msg)
        except ApiError as exc:
            return exc.response()
        return EventStream(_tail(engine, attempt, Path(path)), keepalive=KEEPALIVE)

    del log_tail
