"""Test pruning old history, and keeping what open runs still depend on."""

from datetime import timedelta

import pynet.loop
import pytest
from engine_helpers import TODAY, MakeHarness
from recovery_helpers import make_attempt, make_run, seed

from pyscheduler.engine import retention
from pyscheduler.engine.model import RunState, Trigger
from pyscheduler.store import Batch

ONE_HOUR = timedelta(hours=1)
OLD = TODAY - timedelta(days=100)
MIDDLE = TODAY - timedelta(days=30)
TEN_DAYS_AGO = TODAY - timedelta(days=10)
TWO_DAYS_AGO = TODAY - timedelta(days=2)
YESTERDAY = TODAY - timedelta(days=1)
MANY_OLD = 5
SMALL_BATCH = 2
ALL = 100

PLAIN = """
jobs:
  m:
    command: run-m
    schedule: manual
"""

FAN_IN = """
jobs:
  a: {command: run-a, schedule: manual}
  c: {command: run-c, schedule: manual}
  b:
    command: run-b
    depends_on: [a, c]
"""

YESTERDAYS = """
jobs:
  a: {command: run-a, schedule: manual}
  c: {command: run-c, schedule: manual}
  b:
    command: run-b
    depends_on:
      - {job: a, run_date: -1}
      - c
"""


class TestPrune:
    """Old final runs are deleted from the store and forgotten in memory."""

    def test_prune_deletes_old_final_only(self, harness: MakeHarness) -> None:
        """Delete final runs past ``retention`` at start; keep open and newer ones."""
        h = harness(PLAIN)
        old = make_run("m", RunState.SUCCEEDED, run_date=OLD, attempt=1)
        old_open = make_run("m", RunState.HELD, run_date=OLD, seq=2)
        middle = make_run("m", RunState.SUCCEEDED, run_date=MIDDLE)
        recent = make_run("m", RunState.SUCCEEDED, run_date=YESTERDAY)
        attempt = make_attempt(h, old, pid=None)
        event = h.engine._event(old.run_id, "created")
        runs = [old, old_open, middle, recent]
        seed(h, Batch(runs=runs, attempts=[attempt], events=[event]))

        async def body() -> None:
            """Check the start-up prune, then prune again ten days later."""
            assert await pynet.loop.run_in_thread(h.store.get_run, old.run_id) is None
            assert await pynet.loop.run_in_thread(h.store.attempts_for, old.run_id) == []
            assert await pynet.loop.run_in_thread(h.store.events_for, old.run_id) == []
            for kept in (old_open, middle, recent):
                assert await pynet.loop.run_in_thread(h.store.get_run, kept.run_id)
            assert h.engine.book.get(recent.run_id) is not None
            assert await retention.prune(h.engine, h.clock.now() + 10 * 24 * ONE_HOUR) == 0
            assert h.engine.book.get(recent.run_id) is None
            assert h.engine.book.published(recent.run_id) is None
            assert h.engine.book.get(old_open.run_id) is not None

        h.run_engine(body)

    def test_prune_in_batches_every_hour(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prune at start and then hourly, in batches of ``PRUNE_BATCH``."""
        monkeypatch.setattr(retention, "PRUNE_BATCH", SMALL_BATCH)
        h = harness(PLAIN)
        olds = [make_run("m", RunState.SUCCEEDED, run_date=OLD, seq=n + 1) for n in range(MANY_OLD)]
        seed(h, Batch(runs=olds))

        async def body() -> None:
            """Everything goes at start; a later one goes on the hour."""
            left = await pynet.loop.run_in_thread(h.store.final_run_ids_before, TODAY, ALL)
            assert left == []
            late = make_run("m", RunState.FAILED, run_date=OLD, seq=MANY_OLD + 1)
            await pynet.loop.run_in_thread(h.store.apply, Batch(runs=[late]))
            await h.advance(ONE_HOUR - timedelta(seconds=1))
            assert await pynet.loop.run_in_thread(h.store.get_run, late.run_id) is not None
            await h.advance(timedelta(seconds=1))
            assert await pynet.loop.run_in_thread(h.store.get_run, late.run_id) is None

        h.run_engine(body)

    def test_busy_run_is_left_for_later(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skip a run whose lock is held, without stopping the rest of the prune."""
        monkeypatch.setattr(retention, "PRUNE_BATCH", SMALL_BATCH)
        h = harness(PLAIN)
        olds = [make_run("m", RunState.SUCCEEDED, run_date=OLD, seq=n + 1) for n in range(MANY_OLD)]
        seed(h, Batch(runs=olds))

        async def body() -> None:
            """Hold the first run's lock while pruning."""
            await h.advance(ONE_HOUR)  # (the start-up prune already ran; reseed)
            await pynet.loop.run_in_thread(h.store.apply, Batch(runs=olds))
            assert h.engine._run_locks.try_take(olds[0].run_id)
            try:
                assert await retention.prune(h.engine, h.clock.now()) == MANY_OLD - 1
            finally:
                h.engine._run_locks.give_back(olds[0].run_id)
            assert await pynet.loop.run_in_thread(h.store.get_run, olds[0].run_id)

        h.run_engine(body)


class TestDependenciesKept:
    """Final runs that open runs depend on stay in memory and in the store."""

    def test_eviction_keeps_upstream_of_open_run(self, harness: MakeHarness) -> None:
        """Keep ``a`` (10 days old) in memory while ``b`` still waits on ``c``."""
        h = harness(FAN_IN)
        a = make_run("a", RunState.SUCCEEDED, run_date=TEN_DAYS_AGO)
        b = make_run("b", RunState.WAITING, run_date=TEN_DAYS_AGO, trigger=Trigger.DEPENDENCY)
        seed(h, Batch(runs=[a, b]))

        async def body() -> None:
            """Let the hourly prune run, then finish ``c``: ``b`` must start."""
            await h.advance(ONE_HOUR)
            assert h.engine.book.get(a.run_id) is not None
            await h.engine.trigger("c", run_date=TEN_DAYS_AGO, actor="alice")
            await h.advance(2 * timedelta(minutes=1))
            assert b.run_id in [spec.run_id for spec in h.launcher.launched]

        h.run_engine(body)

    def test_prune_keeps_upstream_of_open_run(self, harness: MakeHarness) -> None:
        """Keep yesterday's ``a`` in the store past a 1-day retention while ``b`` is open."""
        h = harness(YESTERDAYS, settings="retention: 1d")
        a = make_run("a", RunState.SUCCEEDED, run_date=TWO_DAYS_AGO)
        other = make_run("c", RunState.SUCCEEDED, run_date=TWO_DAYS_AGO)
        b = make_run("b", RunState.WAITING, run_date=YESTERDAY, trigger=Trigger.DEPENDENCY)
        seed(h, Batch(runs=[a, other, b]))

        async def body() -> None:
            """Check ``a`` survived the start-up prune and ``c`` of that day did not."""
            assert await pynet.loop.run_in_thread(h.store.get_run, a.run_id) is not None
            assert await pynet.loop.run_in_thread(h.store.get_run, other.run_id) is None
            await h.engine.trigger("c", run_date=YESTERDAY, actor="alice")
            await h.advance(2 * timedelta(minutes=1))
            assert b.run_id in [spec.run_id for spec in h.launcher.launched]

        h.run_engine(body)
