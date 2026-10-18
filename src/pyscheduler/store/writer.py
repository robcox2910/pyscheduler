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

from pynet.loop import Cancelled, Event, Future, Task, get_running_loop, run_in_thread, spawn

from pyscheduler.store.schema import Batch
from pyscheduler.store.store import Store

CLOSED_MESSAGE = "store writer is closed"
NOT_STARTED_MESSAGE = "store writer is not started"


class StoreWriter:
    """Commit saved batches in groups, one transaction per group.

    Attributes:
        commits: How many times the writer has called ``Store.apply``.

    """

    def __init__(self, store: Store) -> None:
        """Prepare a writer for ``store``; call ``start`` before saving."""
        self._store = store
        self._pending: list[tuple[Batch, Future[None]]] = []
        self._wake = Event()
        self._closed = False
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
        waiter of that group).

        Raises:
            RuntimeError: If the writer is closed or not started.

        """
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
        """Refuse new saves, commit everything still pending, then stop."""
        self._closed = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        """Commit whatever is pending, again and again, until closed and empty."""
        while True:
            if not self._pending:
                if self._closed:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            group, self._pending = self._pending, []
            merged = Batch()
            for batch, _ in group:
                merged.merge(batch)
            await self._commit(merged, [done for _, done in group])

    async def _commit(self, merged: Batch, waiters: list[Future[None]]) -> None:
        """Apply one merged batch on a worker thread and tell its waiters."""
        self.commits += 1
        try:
            await run_in_thread(self._store.apply, merged)
        except Cancelled:
            for done in waiters:
                done.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 -- handed to every waiter
            for done in waiters:
                if not done.done():
                    done.set_exception(exc)
            return
        for done in waiters:
            if not done.done():
                done.set_result(None)
