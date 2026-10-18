"""Share one commit between many waiting saves: group commit.

Posting a letter means a walk to the post box. If ten people each walk
there with one letter, that is ten walks. Better: one clerk collects the
letters and takes them all in one trip. Anyone who hands over a letter
while the clerk is out simply goes in the next trip.

``StoreWriter`` is that clerk. ``save`` drops a batch in the tray and
waits. One writer task picks up everything in the tray, merges it into
one batch, and commits it with one PyDB ``Transaction`` (one ``fsync``)
on a worker thread, so the event loop never waits on the disk. Then it
tells every waiter "done" -- or gives them all the same error.
"""

import logging

from pydb.commits import is_committed
from pynet.loop import Cancelled, Event, Future, Task, get_running_loop, run_in_thread, spawn

from pyscheduler.store.schema import Batch
from pyscheduler.store.store import Store

CLOSED_MESSAGE = "store writer is closed"
NOT_STARTED_MESSAGE = "store writer is not started"
FAILED_MESSAGE = "store writer failed"

logger = logging.getLogger(__name__)

type Waiters = list[Future[None]]


class StoreWriter:
    """Commit saved batches in groups, one transaction per group.

    If the writer task itself dies (a bug, not a failed commit), every
    waiting ``save`` gets the error, the writer is marked broken, and
    every later ``save`` raises ``RuntimeError`` at once. ``close`` of a
    broken writer returns quietly: the error was already handed to the
    waiters and logged.

    Attributes:
        commits: How many times the writer has called ``Store.apply``.

    """

    def __init__(self, store: Store) -> None:
        """Prepare a writer for ``store``; call ``start`` before saving."""
        self._store = store
        self._pending: list[tuple[Batch, Future[None]]] = []
        self._in_flight: Waiters = []
        self._wake = Event()
        self._closed = False
        self._broken: BaseException | None = None
        self._task: Task[None] | None = None
        self.commits = 0

    async def start(self) -> None:
        """Start the writer task (once)."""
        if self._task is None and not self._closed:
            self._task = spawn(self._run(), name="store-writer")

    async def save(self, batch: Batch) -> None:
        """Return once ``batch`` is durable in the store.

        An empty batch returns at once. If the group commit that carried
        the batch fails, its error is raised here (and to every other
        waiter of that group). Cancelling the caller while it waits does
        not take the batch back: it is still committed with its group.

        Raises:
            RuntimeError: If the writer is closed, broken or not started.

        """
        if self._broken is not None:
            msg = f"{FAILED_MESSAGE}: {self._broken!r}"
            raise RuntimeError(msg) from self._broken
        if self._closed:
            raise RuntimeError(CLOSED_MESSAGE)
        if self._task is None:
            raise RuntimeError(NOT_STARTED_MESSAGE)
        if batch.is_empty:
            return
        done: Future[None] = Future(get_running_loop())
        self._pending.append((batch, done))
        self._wake.set()
        await done

    async def close(self) -> None:
        """Refuse new saves, commit everything still pending, then stop.

        A commit already in flight is finished first. If the writer task
        is cancelled instead (say, at loop shutdown), its worker thread
        still runs the commit to the end, so that group may be committed
        even though its waiters were told "cancelled".
        """
        self._closed = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        """Run the writer loop; if it dies, fail every waiter and mark it broken."""
        try:
            await self._loop()
        except Cancelled:
            self._fail_everyone(None)
            raise
        except Exception as exc:
            logger.exception("store writer died")
            self._broken = exc
            self._fail_everyone(exc)

    def _fail_everyone(self, exc: Exception | None) -> None:
        """Give every in-flight and pending waiter ``exc`` (``None``: cancel them)."""
        waiters = self._in_flight + [done for _, done in self._pending]
        self._in_flight, self._pending = [], []
        for done in waiters:
            if done.done():
                continue
            if exc is None:
                done.cancel()
            else:
                done.set_exception(exc)

    async def _loop(self) -> None:
        """Commit whatever is pending, again and again, until closed and empty."""
        while True:
            if not self._pending:
                if self._closed:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            group, self._pending = self._pending, []
            self._in_flight = [done for _, done in group]
            merged = Batch()
            try:
                for batch, _ in group:
                    merged.merge(batch)
            except Exception as exc:  # noqa: BLE001 -- a bad batch fails only its group
                _settle(self._in_flight, exc)
            else:
                await self._commit(merged, self._in_flight)
            self._in_flight = []

    async def _commit(self, merged: Batch, waiters: Waiters) -> None:
        """Apply one merged batch on a worker thread and tell its waiters."""
        self.commits += 1
        try:
            await run_in_thread(self._store.apply, merged)
        except Exception as exc:  # noqa: BLE001 -- handed to every waiter
            if not is_committed(exc):
                _settle(waiters, exc)
                return
            logger.warning("store commit is durable but raised afterwards: %r", exc)
        _settle(waiters, None)


def _settle(waiters: Waiters, exc: Exception | None) -> None:
    """Resolve each still-waiting future with success (``None``) or ``exc``."""
    for done in waiters:
        if done.done():
            continue
        if exc is None:
            done.set_result(None)
        else:
            done.set_exception(exc)
