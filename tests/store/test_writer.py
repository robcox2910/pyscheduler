"""Tests for the group-commit writer: many saves, few fsyncs."""

import time
from pathlib import Path

import pynet.loop
import pytest
from store_helpers import make_run

from pyscheduler.store import Batch, Store, StoreWriter

SAVES = 100
MAX_COMMITS = 10
APPLY_NAP = 0.02
BOOM = "disk on fire"


class SlowStore(Store):
    """A store whose commits take a while, so saves pile up behind them."""

    def apply(self, batch: Batch) -> None:
        """Sleep a little in the worker thread, then really commit."""
        time.sleep(APPLY_NAP)
        super().apply(batch)


class FlakyStore(Store):
    """A store whose next commit fails when told to."""

    fail_next = False

    def apply(self, batch: Batch) -> None:
        """Raise once if armed, else really commit."""
        if self.fail_next:
            self.fail_next = False
            raise OSError(BOOM)
        super().apply(batch)


def open_as[S: Store](kind: type[S], tmp_path: Path) -> S:
    """Open a store of the given subclass in ``tmp_path``."""
    store = kind.open(tmp_path / "state", sync=False)
    assert isinstance(store, kind)
    return store


class TestStoreWriter:
    """Saves are durable when they return and share commits under load."""

    def test_group_commit_batches(self, tmp_path: Path) -> None:
        """Commit 100 concurrent saves in far fewer than 100 transactions."""
        store = open_as(SlowStore, tmp_path)
        writer = StoreWriter(store)
        runs = [make_run(seq=n) for n in range(1, SAVES + 1)]

        async def main() -> None:
            """Fire every save at once and wait for them all."""
            await writer.start()
            tasks = [pynet.loop.spawn(writer.save(Batch(runs=[run]))) for run in runs]
            for task in tasks:
                await task
            await writer.close()

        pynet.loop.run(main())
        assert writer.commits < MAX_COMMITS
        store.close()
        reopened = Store.open(tmp_path / "state", sync=False)
        assert len(reopened.query_runs(limit=SAVES * 2)) == SAVES
        reopened.close()

    def test_save_is_durable_on_return(self, tmp_path: Path) -> None:
        """See a saved run in the store as soon as ``save`` returns."""
        store = Store.open(tmp_path / "state", sync=False)
        writer = StoreWriter(store)
        run = make_run()

        async def main() -> None:
            """Save one run, check it, and save an empty batch."""
            await writer.start()
            await writer.save(Batch(runs=[run]))
            assert store.get_run(run.run_id) == run
            await writer.save(Batch())
            await writer.close()

        pynet.loop.run(main())
        assert writer.commits == 1
        store.close()

    def test_failed_commit_raises_to_all_waiters(self, tmp_path: Path) -> None:
        """Give every waiter of a failed group the error; the next save works."""
        store = open_as(FlakyStore, tmp_path)
        writer = StoreWriter(store)
        failures: list[BaseException] = []

        async def save(seq: int) -> None:
            """Save one run and remember any error."""
            try:
                await writer.save(Batch(runs=[make_run(seq=seq)]))
            except OSError as exc:
                failures.append(exc)

        async def main() -> None:
            """Fail one group of three saves, then save again."""
            await writer.start()
            store.fail_next = True
            tasks = [pynet.loop.spawn(save(n)) for n in (1, 2, 3)]
            for task in tasks:
                await task
            await writer.save(Batch(runs=[make_run(seq=4)]))
            await writer.close()

        pynet.loop.run(main())
        assert len(failures) == len((1, 2, 3))
        assert all(str(exc) == BOOM for exc in failures)
        assert [r.run_id for r in store.query_runs()] == [make_run(seq=4).run_id]
        store.close()

    def test_save_after_close_raises(self, tmp_path: Path) -> None:
        """Refuse saves once the writer is closed."""
        store = Store.open(tmp_path / "state", sync=False)
        writer = StoreWriter(store)

        async def main() -> None:
            """Close the writer, then try to save."""
            await writer.start()
            await writer.close()
            await writer.close()
            with pytest.raises(RuntimeError, match="store writer is closed"):
                await writer.save(Batch(runs=[make_run()]))

        pynet.loop.run(main())
        store.close()

    def test_save_before_start_raises(self, tmp_path: Path) -> None:
        """Refuse saves before the writer task is running."""
        store = Store.open(tmp_path / "state", sync=False)
        writer = StoreWriter(store)

        async def main() -> None:
            """Save without starting."""
            with pytest.raises(RuntimeError, match="not started"):
                await writer.save(Batch(runs=[make_run()]))

        pynet.loop.run(main())
        store.close()

    def test_close_flushes_pending(self, tmp_path: Path) -> None:
        """Commit saves still queued when ``close`` is called."""
        store = Store.open(tmp_path / "state", sync=False)
        writer = StoreWriter(store)
        runs = [make_run(seq=n) for n in (1, 2, 3)]

        async def main() -> None:
            """Queue saves and close before they get a turn."""
            await writer.start()
            tasks = [pynet.loop.spawn(writer.save(Batch(runs=[run]))) for run in runs]
            await pynet.loop.sleep(0)
            await writer.close()
            for task in tasks:
                await task

        pynet.loop.run(main())
        assert len(store.query_runs()) == len(runs)
        store.close()
