"""One key per run: a run is changed by one task at a time.

A run pulling into a station is halfway through a change. ``RunLocks``
hands out one key per run, so an attempt's ending and an operator's
button press never move the same run at once; whoever comes second
waits for the key, first come, first served. A key is made when first
wanted and thrown away when nobody holds or waits for it, so idle runs
cost nothing.

An action that needs several keys (a rerun with its downstream runs)
waits only for its first key. It *tries* the others without waiting
(``try_take``), and if one is busy it gives them all back before
waiting, so two such actions can never each hold a key the other wants.
"""

from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from pynet.loop import Cancelled, Future, get_running_loop


@dataclass
class _Key:
    """One run's key: whether it is held, and who waits for it."""

    held: bool = False
    waiters: deque[Future[None]] = field(default_factory=deque[Future[None]])


class RunLocks:
    """Hand out one lock per run id, made lazily and dropped when idle."""

    def __init__(self) -> None:
        """Start with no locks."""
        self._keys: dict[str, _Key] = {}

    def __len__(self) -> int:
        """Return how many runs have a lock held or waited for."""
        return len(self._keys)

    def busy(self, run_id: str) -> bool:
        """Return whether ``run_id``'s lock is held or waited for (without taking it)."""
        key = self._keys.get(run_id)
        return key is not None and (key.held or bool(key.waiters))

    def try_take(self, run_id: str) -> bool:
        """Take ``run_id``'s lock if it is free right now; return whether it was taken."""
        key = self._keys.setdefault(run_id, _Key())
        if key.held or key.waiters:
            return False
        key.held = True
        return True

    def give_back(self, run_id: str) -> None:
        """Release ``run_id``'s lock, handing it to the first waiter if any.

        Raises:
            RuntimeError: If the lock is not held.

        """
        key = self._keys.get(run_id)
        if key is None or not key.held:
            msg = f"lock of {run_id} is not held"
            raise RuntimeError(msg)
        while key.waiters:
            waiter = key.waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)  # stays held: the waiter owns it now
                return
        key.held = False
        del self._keys[run_id]

    async def take(self, run_id: str) -> None:
        """Take ``run_id``'s lock, waiting in line if someone else has it."""
        if self.try_take(run_id):
            return
        key = self._keys[run_id]
        waiter: Future[None] = Future(get_running_loop())
        key.waiters.append(waiter)
        try:
            await waiter
        except Cancelled:
            if waiter.done() and not waiter.cancelled():
                self.give_back(run_id)  # we were handed the key: pass it on
            elif waiter in key.waiters:
                key.waiters.remove(waiter)
            raise

    @asynccontextmanager
    async def hold(self, run_id: str) -> AsyncGenerator[None]:
        """Hold ``run_id``'s lock for the ``async with`` block."""
        await self.take(run_id)
        try:
            yield
        finally:
            self.give_back(run_id)

    async def wait_free(self, run_id: str) -> None:
        """Wait until ``run_id``'s lock can be had (taking and giving it straight back)."""
        async with self.hold(run_id):
            pass
