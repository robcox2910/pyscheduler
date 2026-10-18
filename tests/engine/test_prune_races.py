"""Pruning racing a store read, and eviction of a run an action holds."""

from datetime import timedelta

import pynet.loop
import pytest
from engine_helpers import TODAY, MakeHarness
from recovery_helpers import make_run, seed

from pyscheduler.engine import retention
from pyscheduler.engine.model import Run, RunState
from pyscheduler.store import Batch

OLD = TODAY - timedelta(days=100)
YESTERDAY = TODAY - timedelta(days=1)
TEN_DAYS = timedelta(days=10)

PLAIN = """
jobs:
  m:
    command: run-m
    schedule: manual
"""


class TestPruneRaces:
    """A prune and the things that run beside it."""

    def test_stale_read_does_not_bring_back_a_pruned_run(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store read that saw a run before the prune deleted it does not put it back."""
        h = harness(PLAIN)
        old = make_run("m", RunState.SUCCEEDED, run_date=OLD)
        seed(h, Batch(runs=[old]))

        async def body() -> None:
            """Prune (the start-up round did), then load the day from a stale read."""
            assert await pynet.loop.run_in_thread(h.store.get_run, old.run_id) is None

            def stale(**_: object) -> list[Run]:
                """Answer as the store did before the delete committed."""
                return [old]

            monkeypatch.setattr(h.store, "query_runs", stale)
            assert await h.engine._load_date(OLD, {})
            assert h.engine.book.get(old.run_id) is None
            assert h.engine.book.published(old.run_id) is None

        h.run_engine(body)

    def test_eviction_skips_a_locked_run(self, harness: MakeHarness) -> None:
        """A final run whose lock an action holds stays in memory through eviction."""
        h = harness(PLAIN)
        recent = make_run("m", RunState.SUCCEEDED, run_date=YESTERDAY)
        seed(h, Batch(runs=[recent]))

        async def body() -> None:
            """Hold the run's lock while a prune ten days later evicts old runs."""
            assert h.engine.book.get(recent.run_id) is not None
            assert h.engine._run_locks.try_take(recent.run_id)
            try:
                await retention.prune(h.engine, h.clock.now() + TEN_DAYS)
                assert h.engine.book.get(recent.run_id) is not None
            finally:
                h.engine._run_locks.give_back(recent.run_id)
            await retention.prune(h.engine, h.clock.now() + TEN_DAYS)
            assert h.engine.book.get(recent.run_id) is None

        h.run_engine(body)
