"""The live feed: numbered news of every run change, with catch-up after a gap.

Think of a **radio news desk that keeps its last thousand bulletins on
a clipboard**. Each bulletin gets a number, like ``a1b2c3d4-41``: the
first part names today's shift (the scheduler's boot), the second part
counts up. A listener whose radio dropped out for a moment says "the
last one I heard was 41", and the desk reads out 42 onwards from the
clipboard before going back to live news. If the listener missed more
than the clipboard holds, or names a bulletin from another shift (the
scheduler restarted), the desk cannot fill the gap, so it says just one
thing: ``reset`` -- "start again" -- and the page reloads itself.

The engine feeds the desk through its listener hooks (``attach``):

- ``run`` ``{run_id, job, run_date, state}`` -- a run changed;
- ``attempt`` ``{run_id, attempt, ended}`` -- an attempt started or ended;
- ``reload`` ``{version}`` -- a reload put a new config in force.

Every bulletin is published on the event loop after the change is
durable, so a page never hears of a change the store does not hold yet.

A browser's first connection cannot send ``Last-Event-ID``, so every page
carries the newest id at the moment it was drawn (``data-live-after`` on
``<body>``) and connects to ``/events?after=<id>``: the news that came out
while the page was on its way is replayed too. On a reconnect the browser
sends ``Last-Event-ID``, which wins over ``after``.
"""

import json
import secrets
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping

from pynet.loop import Queue
from pynet.messaging import JsonValue
from pyweb.app import App, CallNext
from pyweb.messages import HttpRequest, Outcome
from pyweb.sse import EventStream, ServerEvent

from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Run

__all__ = ["EMPTY", "KEEPALIVE", "LIVE_BUFFER", "LIVE_PATH", "LIVE_STATE", "LiveFeed", "add_live"]

LIVE_BUFFER = 1000
"""How many recent events are kept for browsers that reconnect."""

KEEPALIVE = 15.0
"""Seconds of quiet before a keep-alive comment keeps the connection open."""

LIVE_PATH = "/events"

BOOT_BYTES = 4
"""Random bytes in a boot id (eight hex digits)."""

LIVE_STATE = "pyscheduler.live_after"
"""``request.state`` key holding the newest event id when the request came in (for pages)."""

MAX_DIGITS = 20
"""The longest event number accepted back; anything longer is garbage (``reset``)."""

RESET = "reset"
EMPTY = "{}"
"""The data of events that carry no data (``reset``, ``end``)."""


class _Listener:
    """One browser's queue of ``(number, event)`` pairs not yet sent."""

    def __init__(self, limit: int) -> None:
        """Hold up to ``limit`` unsent events before giving up on this listener."""
        self.limit = limit
        self.queue: Queue[tuple[int, ServerEvent]] = Queue()

    def offer(self, number: int, event: ServerEvent, reset: ServerEvent) -> None:
        """Queue ``event``; if the listener is a buffer behind, swap its queue for ``reset``."""
        if self.queue.qsize() < self.limit:
            self.queue.put_nowait((number, event))
            return
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait((number, reset))


class LiveFeed:
    """Number the engine's changes, keep the newest, and stream them to browsers.

    Args:
        buffer: How many recent events are kept for replay (``LIVE_BUFFER``).
            A listener that falls this far behind gets a ``reset``.
        boot: The boot id at the start of every event id; by default
            eight random hex digits, new for every start of the scheduler.

    Attributes:
        boot: The boot id.

    ``publish`` must be called on the event loop (the engine's listener
    hooks are).

    """

    def __init__(self, *, buffer: int = LIVE_BUFFER, boot: str | None = None) -> None:
        """Start with no events and nobody listening."""
        self.boot = boot if boot is not None else secrets.token_hex(BOOT_BYTES)
        self._limit = buffer
        self._count = 0
        self._buffer: deque[tuple[int, ServerEvent]] = deque(maxlen=buffer)
        self._listeners: set[_Listener] = set()

    @property
    def last_id(self) -> str:
        """Return the id of the newest event (``<boot>-0`` before the first)."""
        return self._id(self._count)

    @property
    def listeners(self) -> int:
        """Return how many streams are open."""
        return len(self._listeners)

    def _id(self, number: int) -> str:
        """Return the event id for ``number``."""
        return f"{self.boot}-{number}"

    def _reset(self, number: int) -> ServerEvent:
        """Return the ``reset`` event, numbered so a reconnect resumes after ``number``."""
        return ServerEvent(EMPTY, event=RESET, id=self._id(number))

    def publish(self, kind: str, data: Mapping[str, JsonValue]) -> str:
        """Send a ``kind`` event with ``data`` (as JSON) to every listener; return its id."""
        self._count += 1
        number = self._count
        event = ServerEvent(json.dumps(dict(data)), event=kind, id=self._id(number))
        self._buffer.append((number, event))
        reset = self._reset(number)
        for listener in self._listeners:
            listener.offer(number, event, reset)
        return event.id or ""

    def _replay(self, last_event_id: str | None) -> list[tuple[int, ServerEvent]]:
        """Return what a browser that last heard ``last_event_id`` missed, or one ``reset``."""
        if last_event_id is None:
            return []
        boot, _, digits = last_event_id.rpartition("-")
        number_ok = digits.isascii() and digits.isdigit() and len(digits) <= MAX_DIGITS
        if boot != self.boot or not number_ok:
            return [(self._count, self._reset(self._count))]
        heard = int(digits)
        oldest = self._buffer[0][0] if self._buffer else self._count + 1
        if heard > self._count or heard < oldest - 1:
            return [(self._count, self._reset(self._count))]
        return [(number, event) for number, event in self._buffer if number > heard]

    def stream(self, last_event_id: str | None) -> EventStream:
        """Return the stream for a browser that last heard ``last_event_id`` (``None``: new)."""
        return EventStream(self._events(last_event_id), keepalive=KEEPALIVE)

    async def _events(self, last_event_id: str | None) -> AsyncIterator[ServerEvent]:
        """Yield the missed events, then live ones, until the stream is closed.

        Joining and taking the replay happen in one step on the loop, so no
        event can fall between them; the number check drops any repeat.
        """
        listener = _Listener(self._limit)
        self._listeners.add(listener)
        try:
            sent = 0
            for number, event in self._replay(last_event_id):
                sent = number
                yield event
            while True:
                number, event = await listener.queue.get()
                if number > sent:
                    sent = number
                    yield event
        finally:
            self._listeners.discard(listener)

    def attach(self, engine: Engine) -> Callable[[], None]:
        """Publish ``engine``'s run, attempt and reload changes; return the detach function.

        Attach before ``engine.start()`` so the changes recovery makes are heard.
        """

        def on_run(run: Run) -> None:
            """Publish a ``run`` event."""
            self.publish(
                "run",
                {
                    "run_id": run.run_id,
                    "job": run.job,
                    "run_date": run.run_date.isoformat(),
                    "state": run.state.value,
                },
            )

        def on_attempt(attempt: Attempt) -> None:
            """Publish an ``attempt`` event."""
            self.publish(
                "attempt",
                {
                    "run_id": attempt.run_id,
                    "attempt": attempt.attempt,
                    "ended": attempt.ended_at is not None,
                },
            )

        def on_reload() -> None:
            """Publish a ``reload`` event with the version now in force."""
            self.publish("reload", {"version": engine.config_version})

        undo = [
            engine.subscribe(on_run),
            engine.watch_attempts(on_attempt),
            engine.watch_reloads(on_reload),
        ]

        def detach() -> None:
            """Stop publishing the engine's changes (a second call does nothing)."""
            for unsubscribe in undo:
                unsubscribe()

        return detach


def add_live(app: App, live: LiveFeed) -> None:
    """Add ``GET /events?after=``, and note the newest id on each request for the pages.

    The feed resumes after the ``Last-Event-ID`` header (a reconnect) or,
    without it, after the ``after`` query (a page's first connect).
    """

    @app.middleware
    async def note_newest(request: HttpRequest, call_next: CallNext) -> Outcome:
        """Remember the newest id before the page reads anything (so nothing is skipped)."""
        request.state[LIVE_STATE] = live.last_id
        return await call_next(request)

    @app.get(LIVE_PATH)
    async def events(request: HttpRequest) -> EventStream:
        """Stream the feed (the access layer has checked the caller)."""
        last = request.last_event_id
        return live.stream(request.query.get("after") if last is None else last)

    del note_newest, events
