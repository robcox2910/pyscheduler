"""Test catch-up of missed firings, clock jumps, and pruning old history."""

import logging
from datetime import timedelta

import pynet.loop
import pytest
from engine_helpers import START, TODAY, MakeHarness
from recovery_helpers import make_attempt, make_run, restarted, run_at, seed

from pyscheduler.engine import recovery
from pyscheduler.engine.model import RunState, Trigger
from pyscheduler.store import Batch

ONE_HOUR = timedelta(hours=1)
FIVE_MINUTES = timedelta(minutes=5)
LATER = START + 3 * ONE_HOUR + 30 * timedelta(minutes=1)
TRUNCATED = 2
OLD = TODAY - timedelta(days=100)
MIDDLE = TODAY - timedelta(days=30)
YESTERDAY = TODAY - timedelta(days=1)
MANY_OLD = 5
SMALL_BATCH = 2

HOURLY = """
jobs:
  h:
    command: run-h
    schedule: "0 * * * *"
    catch_up: {policy}
  off:
    command: run-off
    schedule: "0 * * * *"
    catch_up: all
    enabled: false
  m:
    command: run-m
    schedule: manual
"""


def last_tick_batch(at: object) -> Batch:
    """Return a batch that sets the stored ``last_tick``."""
    return Batch(engine={"last_tick": str(at)})


class TestCatchUp:
    """Missed firings follow each job's ``catch_up`` policy."""

    @pytest.mark.parametrize(("policy", "expected"), [("latest", 1), ("all", 3), ("none", 0)])
    def test_catch_up_policies(self, harness: MakeHarness, policy: str, expected: int) -> None:
        """Catch up 3 missed hourly firings as ``latest`` (1), ``all`` (3) or ``none`` (0)."""
        h = harness(HOURLY.format(policy=policy))
        seed(h, last_tick_batch(START.isoformat()))

        async def body() -> None:
            """Count the caught-up runs."""
            runs = h.runs("h")
            assert len(runs) == expected
            assert all(run.trigger is Trigger.CATCH_UP for run in runs)
            if runs:
                assert runs[-1].scheduled_at == START + 3 * ONE_HOUR
            assert h.runs("off") == []
            tick = await pynet.loop.run_in_thread(h.store.get_engine, "last_tick")
            assert tick == LATER.isoformat()
            await h.advance_to(START + 4 * ONE_HOUR)
            assert h.run("h").trigger is Trigger.SCHEDULE

        run_at(h, LATER, body)

    def test_first_start_catches_nothing(self, harness: MakeHarness) -> None:
        """Catch nothing up without a ``last_tick``, but remember the start."""
        h = harness(HOURLY.format(policy="all"))

        async def body() -> None:
            """No runs, and ``last_tick`` is now."""
            assert h.runs("h") == []
            tick = await pynet.loop.run_in_thread(h.store.get_engine, "last_tick")
            assert tick == LATER.isoformat()

        run_at(h, LATER, body)

    def test_all_is_bounded(
        self,
        harness: MakeHarness,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Keep only the latest ``MAX_CATCH_UP`` firings, with a warning."""
        monkeypatch.setattr(recovery, "MAX_CATCH_UP", TRUNCATED)
        h = harness(HOURLY.format(policy="all"))
        seed(h, last_tick_batch(START.isoformat()))

        async def body() -> None:
            """Two runs: 02:00 and 03:00."""
            times = [run.scheduled_at for run in h.runs("h")]
            assert times == [START + 2 * ONE_HOUR, START + 3 * ONE_HOUR]

        with caplog.at_level(logging.WARNING):
            run_at(h, LATER, body)
        assert "missed 3 firings" in caplog.text

    def test_no_duplicate_after_restart(self, harness: MakeHarness) -> None:
        """Never create a firing twice, even when it is replayed after a restart."""
        h = harness(HOURLY.format(policy="all"))

        async def first() -> None:
            """Fire 00:00 and 01:00."""
            await h.advance_to(START + ONE_HOUR + FIVE_MINUTES)

        h.run_engine(first)
        again = restarted(h)
        seed(again, last_tick_batch((START - FIVE_MINUTES).isoformat()))

        async def second() -> None:
            """Nothing new for the replayed window."""
            assert [run.scheduled_at for run in again.runs("h")] == [START, START + ONE_HOUR]
            assert all(run.trigger is Trigger.SCHEDULE for run in again.runs("h"))

        again.run_engine(second)
        with again.reopened() as store:
            assert len(store.query_runs(job="h")) == len([START, START + ONE_HOUR])

    def test_clock_jump_applies_policy(self, harness: MakeHarness) -> None:
        """Apply ``catch_up`` to firings a forward clock jump passed over."""
        h = harness(HOURLY.format(policy="latest"))
        jump = START + 5 * ONE_HOUR + FIVE_MINUTES

        async def body() -> None:
            """Jump the wall clock and wake the firing task."""
            await h.advance(FIVE_MINUTES)
            h.clock._now = jump
            await h.advance_to(jump)
            runs = h.runs("h")
            assert [(r.scheduled_at, r.trigger) for r in runs] == [
                (START, Trigger.SCHEDULE),
                (START + 5 * ONE_HOUR, Trigger.CATCH_UP),
            ]
            await h.advance_to(START + 6 * ONE_HOUR)
            assert h.run("h").trigger is Trigger.SCHEDULE

        h.run_engine(body)


class TestPrune:
    """Old final runs are deleted from the store and forgotten in memory."""

    def test_prune_deletes_old_final_only(self, harness: MakeHarness) -> None:
        """Delete final runs past ``retention``; keep open ones and newer ones."""
        h = harness(HOURLY.format(policy="none"))
        old = make_run("m", RunState.SUCCEEDED, run_date=OLD, attempt=1)
        old_open = make_run("m", RunState.HELD, run_date=OLD, seq=2)
        middle = make_run("m", RunState.SUCCEEDED, run_date=MIDDLE)
        recent = make_run("m", RunState.SUCCEEDED, run_date=YESTERDAY)
        attempt = make_attempt(h, old, pid=None)
        event = h.engine._event(old.run_id, "created")
        runs = [old, old_open, middle, recent]
        seed(h, Batch(runs=runs, attempts=[attempt], events=[event]))

        async def body() -> None:
            """Prune now, then again ten days later."""
            assert h.engine.book.get(recent.run_id) is not None
            assert await recovery.prune(h.engine, h.clock.now()) == 1
            assert await pynet.loop.run_in_thread(h.store.get_run, old.run_id) is None
            assert await pynet.loop.run_in_thread(h.store.attempts_for, old.run_id) == []
            assert await pynet.loop.run_in_thread(h.store.events_for, old.run_id) == []
            for kept in (old_open, middle, recent):
                assert await pynet.loop.run_in_thread(h.store.get_run, kept.run_id)
            assert await recovery.prune(h.engine, h.clock.now() + 10 * 24 * ONE_HOUR) == 0
            assert h.engine.book.get(recent.run_id) is None
            assert h.engine.book.published(recent.run_id) is None
            assert h.engine.book.get(old_open.run_id) is not None

        h.run_engine(body)

    def test_prune_in_batches_every_hour(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prune hourly, in batches of ``PRUNE_BATCH``."""
        monkeypatch.setattr(recovery, "PRUNE_BATCH", SMALL_BATCH)
        h = harness(HOURLY.format(policy="none"))
        olds = [make_run("m", RunState.SUCCEEDED, run_date=OLD, seq=n + 1) for n in range(MANY_OLD)]
        seed(h, Batch(runs=olds))

        async def body() -> None:
            """Nothing goes before the hour; everything after it."""
            count = await pynet.loop.run_in_thread(h.store.final_run_ids_before, TODAY, 100)
            assert len(count) == MANY_OLD
            await h.advance(ONE_HOUR)
            assert await pynet.loop.run_in_thread(h.store.final_run_ids_before, TODAY, 100) == []
            late = make_run("m", RunState.FAILED, run_date=OLD, seq=MANY_OLD + 1)
            await pynet.loop.run_in_thread(h.store.apply, Batch(runs=[late]))
            await h.advance(ONE_HOUR)
            assert await pynet.loop.run_in_thread(h.store.get_run, late.run_id) is None

        h.run_engine(body)
