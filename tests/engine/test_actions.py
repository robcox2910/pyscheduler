"""Test operator actions on runs: rerun, hold and release, kill."""

from datetime import timedelta

import pytest
from action_helpers import (
    ALICE,
    AT_1830,
    CHAIN,
    CONFLICT,
    FOUR_HOURS,
    MANUAL,
    ONE_MINUTE,
    TEN_MINUTES,
    THREE_ATTEMPTS,
    TUESDAY,
    TWO_ATTEMPTS,
    launched,
    status_of,
)
from engine_helpers import MakeHarness

from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import RunState
from pyscheduler.testing import FakeResult


class TestRerun:
    """Rerun reopens final runs, optionally with their downstream runs."""

    def test_rerun_downstream(self, harness: MakeHarness) -> None:
        """Reopen ``a`` and ``b``; ``a`` runs (with a fresh retry budget), then ``b``."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(), FakeResult(exit_code=1), FakeResult())

        async def body() -> None:
            """Let the chain succeed, then rerun ``a`` with its downstream."""
            await h.advance_to(AT_1830 + timedelta(minutes=3))
            assert h.run("b").state is RunState.SUCCEEDED
            runs = await h.engine.rerun("a.2026-10-19.1", downstream=True, actor=ALICE)
            assert [run.run_id for run in runs] == ["a.2026-10-19.1", "b.2026-10-19.1"]
            assert h.run("b").state is RunState.WAITING
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("a").state is RunState.SUCCEEDED
        assert h.run("a").attempt == THREE_ATTEMPTS
        assert h.run("b").state is RunState.SUCCEEDED
        assert h.run("b").attempt == TWO_ATTEMPTS
        assert launched(h)[-1] == "b.2026-10-19.1"
        assert h.run("a").reason == ""

    def test_rerun_without_downstream_leaves_final_downstream(self, harness: MakeHarness) -> None:
        """Rerun only ``a`` when ``downstream`` is not asked for."""
        h = harness(CHAIN)

        async def body() -> None:
            """Let the chain succeed, then rerun ``a`` alone."""
            await h.advance_to(AT_1830 + timedelta(minutes=3))
            runs = await h.engine.rerun("a.2026-10-19.1", actor=ALICE)
            assert [run.run_id for run in runs] == ["a.2026-10-19.1"]
            assert runs[0].rerun == 1
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("b").attempt == 1

    def test_rerun_non_final_409(self, harness: MakeHarness) -> None:
        """Refuse to rerun a run that is not final."""
        h = harness(CHAIN)

        async def body() -> None:
            """Try to rerun the waiting ``b``."""
            await h.advance_to(AT_1830)
            with pytest.raises(ActionError) as caught:
                await h.engine.rerun("b.2026-10-19.1", actor=ALICE)
            assert status_of(caught) == CONFLICT

        h.run_engine(body)

    def test_rerun_evicted_run_loads_from_store(self, harness: MakeHarness) -> None:
        """Rerun a final run no longer in memory by loading it from the store."""
        h = harness(CHAIN)

        async def body() -> None:
            """Let the chain succeed, forget it, then rerun ``a`` with downstream."""
            await h.advance_to(AT_1830 + timedelta(minutes=3))
            h.engine.book.evict_final_before(TUESDAY)
            assert h.engine.book.get("a.2026-10-19.1") is None
            runs = await h.engine.rerun("a.2026-10-19.1", downstream=True, actor=ALICE)
            assert len(runs) == TWO_ATTEMPTS
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("b").state is RunState.SUCCEEDED
        assert h.run("b").attempt == TWO_ATTEMPTS


class TestHold:
    """Hold freezes a run; release puts it back where it would be."""

    def test_hold_release_returns_state(self, harness: MakeHarness) -> None:
        """Keep a held run held while its dependency succeeds; release queues and runs it."""
        h = harness(CHAIN)

        async def body() -> None:
            """Hold ``b``, let ``a`` succeed, release ``b``."""
            await h.advance_to(AT_1830)
            held = await h.engine.hold("b.2026-10-19.1", actor=ALICE)
            assert held.state is RunState.HELD
            assert held.held_from is RunState.WAITING
            await h.advance(TEN_MINUTES)
            assert h.run("a").state is RunState.SUCCEEDED
            assert h.run("b").state is RunState.HELD
            released = await h.engine.release("b.2026-10-19.1", actor=ALICE)
            assert released.state is RunState.QUEUED
            assert released.held_from is None
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("b").state is RunState.SUCCEEDED

    def test_hold_retry_wait_then_release_queues_now(self, harness: MakeHarness) -> None:
        """Cancel the retry timer on hold; release queues the run at once."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(exit_code=1), FakeResult())

        async def body() -> None:
            """Fail once, hold during the retry wait, wait past it, release."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.advance(ONE_MINUTE)
            assert h.run("m").state is RunState.RETRY_WAIT
            await h.engine.hold(run.run_id, actor=ALICE)
            await h.advance(timedelta(hours=1))
            assert len(h.launcher.launched) == 1
            released = await h.engine.release(run.run_id, actor=ALICE)
            assert released.state is RunState.QUEUED
            await h.advance(ONE_MINUTE)

        h.run_engine(body)
        assert h.run("m").state is RunState.SUCCEEDED
        assert h.run("m").attempt == TWO_ATTEMPTS

    def test_hold_and_release_illegal_states(self, harness: MakeHarness) -> None:
        """Refuse to hold a running run or release one that is not held."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Try both on the running ``a`` and the waiting ``b``."""
            await h.advance_to(AT_1830)
            with pytest.raises(ActionError) as caught:
                await h.engine.hold("a.2026-10-19.1", actor=ALICE)
            assert status_of(caught) == CONFLICT
            with pytest.raises(ActionError) as caught:
                await h.engine.release("b.2026-10-19.1", actor=ALICE)
            assert status_of(caught) == CONFLICT

        h.run_engine(body)

    def test_hold_queued_lets_overlap_waiter_go(self, harness: MakeHarness) -> None:
        """Holding a queued run takes it out of the queue so the pool slot goes elsewhere."""
        h = harness(
            """
            pools: {db: 1}
            jobs:
              x:
                command: run-x
                schedule: manual
                pools: {db: 1}
              y:
                command: run-y
                schedule: manual
                pools: {db: 1}
            """
        )
        h.launcher.plan("x", FakeResult(duration=TEN_MINUTES))

        async def body() -> None:
            """Fill the pool, queue ``y``, hold it, let ``x`` end, release ``y``."""
            await h.engine.trigger("x", actor=ALICE)
            y = await h.engine.trigger("y", actor=ALICE)
            assert y.state is RunState.QUEUED
            await h.engine.hold(y.run_id, actor=ALICE)
            assert y.run_id not in h.engine.ready
            await h.advance(TEN_MINUTES)
            assert launched(h) == ["x.2026-10-19.1"]
            await h.engine.release(y.run_id, actor=ALICE)
            await h.advance(ONE_MINUTE)

        h.run_engine(body)
        assert h.run("y").state is RunState.SUCCEEDED


class TestKill:
    """Kill ends a running attempt with no retry."""

    def test_kill_no_retry(self, harness: MakeHarness) -> None:
        """End a run with three retries as failed after one killed attempt."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(duration=FOUR_HOURS, ignores_term=True))

        async def body() -> None:
            """Start ``m``, kill it, and wait past the grace."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            killed = await h.engine.kill(run.run_id, actor=ALICE)
            assert killed.state is RunState.RUNNING
            await h.advance(timedelta(seconds=5))
            assert h.run("m").state is RunState.RUNNING  # ignores SIGTERM
            await h.advance(timedelta(seconds=10))

        h.run_engine(body)
        run = h.run("m")
        assert run.state is RunState.FAILED
        assert run.reason == "killed by alice"
        with h.reopened() as store:
            assert len(store.attempts_for(run.run_id)) == 1

    def test_kill_mid_launch(self, harness: MakeHarness) -> None:
        """Kill an attempt that has no handle yet as soon as it gets one."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Kill straight after the trigger, before the launch finishes."""
            run = await h.engine.trigger("m", actor=ALICE)
            assert h.run("m").state is RunState.RUNNING
            assert run.run_id not in h.engine._handles
            await h.engine.kill(run.run_id, actor=ALICE)
            await h.engine.settle()

        h.run_engine(body)
        assert h.run("m").state is RunState.FAILED
        assert h.run("m").reason == "killed by alice"

    def test_kill_not_running_409(self, harness: MakeHarness) -> None:
        """Refuse to kill a run that is not running."""
        h = harness(CHAIN)

        async def body() -> None:
            """Try to kill the waiting ``b``."""
            await h.advance_to(AT_1830)
            with pytest.raises(ActionError) as caught:
                await h.engine.kill("b.2026-10-19.1", actor=ALICE)
            assert status_of(caught) == CONFLICT

        h.run_engine(body)
