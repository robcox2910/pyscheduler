"""Tell the time, and wake things up at a given instant.

The engine never asks the computer for the time directly. It asks a
``Clock``. In production that is ``SystemClock``, the real wall clock. In
tests it is ``FakeClock``, a clock with no battery: its hands move only
when the test calls ``advance``. A test can then jump a whole day ahead in
a microsecond and check exactly what happened at each moment.

Both clocks speak in aware UTC ``datetime`` instants and run on PyNet's own
event loop.
"""

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pynet.loop
from pynet.loop import EventLoop, Future, Handle

MAX_NAP = 60.0
"""The longest real timer ``SystemClock`` sets before checking the wall clock again."""

SETTLE_TURNS = 5
"""How many loop turns ``FakeClock`` yields after each callback so woken tasks run."""

NAIVE_MESSAGE = "FakeClock needs an aware datetime"


class Cancel(Protocol):
    """Something that can call off a scheduled callback."""

    def cancel(self) -> None:
        """Call off the callback; do nothing if it already ran."""
        ...


class Clock(Protocol):
    """Tell the time and call things back at an instant."""

    def now(self) -> datetime:
        """Return the current instant as an aware UTC datetime."""
        ...

    def call_at(self, when: datetime, callback: Callable[[], None]) -> Cancel:
        """Call ``callback`` once the time reaches ``when``."""
        ...

    async def sleep_until(self, when: datetime) -> None:
        """Pause the current task until the time reaches ``when``."""
        ...


async def _sleep_until(clock: Clock, when: datetime) -> None:
    """Sleep on ``clock`` until ``when`` using a future and ``call_at``."""
    if when <= clock.now():
        await pynet.loop.sleep(0)
        return
    future: Future[None] = Future(pynet.loop.get_running_loop())

    def wake() -> None:
        """Fill the future unless it was cancelled meanwhile."""
        if not future.done():
            future.set_result(None)

    handle = clock.call_at(when, wake)
    try:
        await future
    finally:
        handle.cancel()


class _SystemTimer:
    """A wall-clock callback, re-armed in naps of at most ``MAX_NAP`` seconds.

    The loop's timers run on the monotonic clock, which does not follow
    the wall clock when it is changed (or when a laptop sleeps). Napping
    and re-checking keeps the callback close to the wall-clock instant.
    """

    def __init__(
        self, clock: SystemClock, loop: EventLoop, when: datetime, callback: Callable[[], None]
    ) -> None:
        """Arm the first nap."""
        self._clock = clock
        self._loop = loop
        self._when = when
        self._callback = callback
        self._cancelled = False
        self._handle: Handle = self._arm()

    def _arm(self) -> Handle:
        """Set a loop timer for the rest of the wait, capped at ``MAX_NAP``."""
        remaining = (self._when - self._clock.now()).total_seconds()
        if remaining <= 0:
            return self._loop.call_soon(self._check)
        return self._loop.call_later(min(remaining, MAX_NAP), self._check)

    def _check(self) -> None:
        """Run the callback if its instant has come, else nap again."""
        if self._cancelled:
            return
        if self._clock.now() >= self._when:
            self._cancelled = True
            self._callback()
        else:
            self._handle = self._arm()

    def cancel(self) -> None:
        """Call off the callback."""
        self._cancelled = True
        self._handle.cancel()


class SystemClock:
    """The real wall clock, on the running pynet loop."""

    def now(self) -> datetime:
        """Return the current wall-clock instant in UTC."""
        return datetime.now(UTC)

    def call_at(self, when: datetime, callback: Callable[[], None]) -> Cancel:
        """Call ``callback`` on the running loop once the wall clock reaches ``when``."""
        return _SystemTimer(self, pynet.loop.get_running_loop(), when, callback)

    async def sleep_until(self, when: datetime) -> None:
        """Pause the current task until the wall clock reaches ``when``."""
        await _sleep_until(self, when)


@dataclass(order=True)
class _FakeTimer:
    """A callback waiting for the fake clock, ordered by (when, arrival)."""

    when: datetime
    seq: int
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def cancel(self) -> None:
        """Call off the callback."""
        self.cancelled = True


class FakeClock:
    """A clock whose hands move only when a test calls ``advance``.

    Args:
        start: The instant the clock shows at first; must be aware.

    Raises:
        ValueError: If ``start`` has no time zone.

    """

    def __init__(self, start: datetime) -> None:
        """Set the clock to ``start``."""
        if start.tzinfo is None:
            raise ValueError(NAIVE_MESSAGE)
        self._now = start.astimezone(UTC)
        self._timers: list[_FakeTimer] = []
        self._seq = itertools.count()
        self._advancing = False

    def now(self) -> datetime:
        """Return the instant the clock shows."""
        return self._now

    def call_at(self, when: datetime, callback: Callable[[], None]) -> Cancel:
        """Call ``callback`` when the clock reaches ``when``.

        A callback that is already due runs on the next loop turns, without
        an ``advance``.
        """
        timer = _FakeTimer(when, next(self._seq), callback)
        heapq.heappush(self._timers, timer)
        if when <= self._now and not self._advancing:
            pynet.loop.get_running_loop().call_soon(self._fire_due)
        return timer

    async def sleep_until(self, when: datetime) -> None:
        """Pause the current task until the clock reaches ``when``."""
        await _sleep_until(self, when)

    async def advance(self, by: timedelta) -> None:
        """Move the clock forward by ``by``, firing callbacks on the way."""
        await self.advance_to(self._now + by)

    async def advance_to(self, when: datetime) -> None:
        """Move the clock forward to ``when``, firing callbacks on the way.

        Callbacks fire one at a time in time order (ties in the order they
        were added), with the clock showing each callback's own time. After
        each, the loop gets ``SETTLE_TURNS`` turns so woken tasks can run;
        callbacks they add inside the window fire too. The clock never
        moves backwards.
        """
        self._advancing = True
        try:
            while True:
                timer = self._pop_due(when)
                if timer is None:
                    break
                self._now = max(self._now, timer.when)
                timer.callback()
                await self._settle()
        finally:
            self._advancing = False
        self._now = max(self._now, when)
        await self._settle()

    def _pop_due(self, until: datetime) -> _FakeTimer | None:
        """Remove and return the next live timer due by ``until``, if any."""
        while self._timers:
            timer = self._timers[0]
            if timer.cancelled:
                heapq.heappop(self._timers)
                continue
            if timer.when > until:
                return None
            heapq.heappop(self._timers)
            timer.cancelled = True  # spent: a late cancel() is harmless
            return timer
        return None

    def _fire_due(self) -> None:
        """Run every live callback that is due now (outside ``advance``)."""
        while (timer := self._pop_due(self._now)) is not None:
            timer.callback()

    @staticmethod
    async def _settle() -> None:
        """Give the loop a few turns so tasks woken by a callback can run."""
        for _ in range(SETTLE_TURNS):
            await pynet.loop.sleep(0)
