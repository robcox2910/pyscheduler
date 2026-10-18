"""Test actions that meet a run halfway through another change (the per-run lock)."""

from collections.abc import Callable
from datetime import timedelta

import pynet.loop
import pytest
from action_helpers import ALICE, CONFLICT, POLL, Gate, actor_events, status_of
from engine_helpers import Harness, MakeHarness

from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import RunState
from pyscheduler.store import Batch
from pyscheduler.testing import FakeResult

ONE_MINUTE = timedelta(minutes=1)
HALF_HOUR = timedelta(minutes=30)
TWO_HOURS = timedelta(hours=2)
TWO = 2

POOLED = """
pools: {db: 1}
jobs:
  m:
    command: run-m
    schedule: manual
    pools: {db: 1}
    retries: {count: 3, delay: 10m}
  o:
    command: run-o
    schedule: manual
    pools: {db: 1}
"""


def with_state(state: RunState) -> Callable[[Batch], bool]:
    """Return a test matching batches that carry a run in ``state``."""

    def matches(batch: Batch) -> bool:
        """Match a batch with a run in ``state``."""
        return any(run.state is state for run in batch.runs)

    return matches


def assert_slots_free(h: Harness) -> None:
    """Check no slot or pool amount is still taken, and the pool was never overrun."""
    assert h.launcher.max_concurrent == 1
    assert len(h.engine._run_locks) == 0
    assert h.engine.admission.running == 0
    assert h.engine.admission.in_use("db") == 0


def m_launches(h: Harness) -> int:
    """Return how many attempts of ``m`` were launched."""
    return sum(spec.run_id.startswith("m.") for spec in h.launcher.launched)


class TestDuringAnEnding:
    """An action waits for an attempt's ending commit, then sees the ended run."""

    def test_rerun_during_success_commit(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rerun once the success is durable; the slots stay exact."""
        h = harness(POOLED)
        h.launcher.plan("m", FakeResult(), FakeResult(duration=HALF_HOUR))
        gate = Gate(h, monkeypatch, with_state(RunState.SUCCEEDED))

        async def body() -> None:
            """Hold the success commit, rerun meanwhile, then let both go."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            moving = pynet.loop.spawn(h.clock.advance_to(h.clock.now() + ONE_MINUTE))
            await gate.wait_reached()
            rerun = pynet.loop.spawn(h.engine.rerun(run.run_id, actor=ALICE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await moving
            await rerun
            await h.engine.settle()
            assert h.engine.admission.in_use("db") == 1
            other = await h.engine.trigger("o", actor=ALICE)
            assert other.state is RunState.QUEUED
            await h.advance(TWO_HOURS)

        h.run_engine(body)
        assert h.run("o").state is RunState.SUCCEEDED
        assert m_launches(h) == TWO
        assert_slots_free(h)

    def test_force_start_during_retry_commit(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force-start a run once its retry wait is durable, taking exactly one slot."""
        h = harness(POOLED)
        h.launcher.plan("m", FakeResult(exit_code=1), FakeResult(duration=HALF_HOUR))
        gate = Gate(h, monkeypatch, with_state(RunState.RETRY_WAIT))

        async def body() -> None:
            """Hold the retry commit, force-start meanwhile."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            moving = pynet.loop.spawn(h.clock.advance_to(h.clock.now() + ONE_MINUTE))
            await gate.wait_reached()
            forced = pynet.loop.spawn(h.engine.force_start(run.run_id, actor=ALICE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await moving
            await forced
            await h.engine.settle()
            assert h.run("m").state is RunState.RUNNING
            other = await h.engine.trigger("o", actor=ALICE)
            assert other.state is RunState.QUEUED
            await h.advance(TWO_HOURS)

        h.run_engine(body)
        assert m_launches(h) == TWO
        assert h.run("o").state is RunState.SUCCEEDED
        assert_slots_free(h)

    def test_hold_release_during_retry_commit(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hold a run once its retry wait is durable; release queues it with one slot."""
        h = harness(POOLED)
        h.launcher.plan("m", FakeResult(exit_code=1), FakeResult())
        gate = Gate(h, monkeypatch, with_state(RunState.RETRY_WAIT))

        async def body() -> None:
            """Hold during the retry commit, then release."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            moving = pynet.loop.spawn(h.clock.advance_to(h.clock.now() + ONE_MINUTE))
            await gate.wait_reached()
            holding = pynet.loop.spawn(h.engine.hold(run.run_id, actor=ALICE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await moving
            held = await holding
            assert held.held_from is RunState.RETRY_WAIT
            assert_slots_free(h)
            await h.engine.release(run.run_id, actor=ALICE)
            await h.advance(TWO_HOURS)

        h.run_engine(body)
        assert h.run("m").state is RunState.SUCCEEDED
        assert m_launches(h) == TWO
        assert_slots_free(h)


class TestKillRaces:
    """A kill and an attempt's ending never cross."""

    def test_attempt_ends_during_kill_commit(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End a run killed while its attempt ended as killed, with no retry."""
        h = harness(POOLED)
        h.launcher.plan("m", FakeResult(exit_code=1), FakeResult(duration=HALF_HOUR))
        gate = Gate(h, monkeypatch, lambda b: any(e.action == "kill" for e in b.events))

        async def body() -> None:
            """Hold the kill commit while the attempt fails."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            killing = pynet.loop.spawn(h.engine.kill(run.run_id, actor=ALICE))
            await gate.wait_reached()
            moving = pynet.loop.spawn(h.clock.advance_to(h.clock.now() + ONE_MINUTE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await killing
            await moving
            await h.advance(TWO_HOURS)

        h.run_engine(body)
        assert h.run("m").state is RunState.FAILED
        assert h.run("m").reason == "killed by alice"
        assert m_launches(h) == 1
        assert_slots_free(h)

    def test_kill_after_the_attempt_ended_409(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse a kill that waited for an ending: the run is no longer running."""
        h = harness(POOLED)
        h.launcher.plan("m", FakeResult(exit_code=1), FakeResult())
        gate = Gate(h, monkeypatch, with_state(RunState.RETRY_WAIT))

        async def body() -> None:
            """Hold the retry commit and kill meanwhile."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            moving = pynet.loop.spawn(h.clock.advance_to(h.clock.now() + ONE_MINUTE))
            await gate.wait_reached()
            killing = pynet.loop.spawn(h.engine.kill(run.run_id, actor=ALICE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await moving
            with pytest.raises(ActionError) as caught:
                await killing
            assert status_of(caught) == CONFLICT
            await h.advance(TWO_HOURS)

        h.run_engine(body)
        assert h.run("m").state is RunState.SUCCEEDED
        assert_slots_free(h)

    def test_double_kill(self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        """Answer the second of two concurrent kills with 409, and record one kill."""
        h = harness(POOLED)
        h.launcher.plan("m", FakeResult(duration=HALF_HOUR, ignores_term=True))
        gate = Gate(h, monkeypatch, lambda b: any(e.action == "kill" for e in b.events))

        async def body() -> None:
            """Kill twice while the first kill's commit is held."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            first = pynet.loop.spawn(h.engine.kill(run.run_id, actor=ALICE))
            await gate.wait_reached()
            second = pynet.loop.spawn(h.engine.kill(run.run_id, actor=ALICE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await first
            with pytest.raises(ActionError) as caught:
                await second
            assert status_of(caught) == CONFLICT
            await h.advance(ONE_MINUTE)

        h.run_engine(body)
        assert h.run("m").state is RunState.FAILED
        kills = [e for e in actor_events(h, "m.2026-10-19.1") if e.action == "kill"]
        assert len(kills) == 1
        assert_slots_free(h)


class TestDownstreamLocks:
    """Two downstream reruns never wait for each other in a circle."""

    def test_crossing_downstream_reruns(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finish both reruns of ``a`` and ``b`` (both reaching ``c``) without deadlock."""
        h = harness(
            """
            jobs:
              a: {command: run-a, schedule: manual}
              b: {command: run-b, depends_on: [a]}
              c: {command: run-c, depends_on: [a, b]}
            """
        )
        h.launcher.plan("b", FakeResult(duration=TWO_HOURS), FakeResult())
        gate = Gate(
            h,
            monkeypatch,
            lambda b: any(r.job == "c" and r.state is RunState.SUCCEEDED for r in b.runs),
        )

        async def body() -> None:
            """Hold ``c``'s lock in a mark, start both reruns, then let go."""
            first = await h.engine.trigger("a", actor=ALICE)
            await h.advance(ONE_MINUTE)
            b_run = h.runs("b")[0]
            c_run = h.runs("c")[0]
            marking = pynet.loop.spawn(h.engine.mark_success(c_run.run_id, actor=ALICE))
            await gate.wait_reached()
            rerun_a = pynet.loop.spawn(h.engine.rerun(first.run_id, downstream=True, actor=ALICE))
            await pynet.loop.sleep(POLL)
            moving = pynet.loop.spawn(h.clock.advance_to(h.clock.now() + TWO_HOURS + ONE_MINUTE))
            await pynet.loop.sleep(POLL)
            rerun_b = pynet.loop.spawn(h.engine.rerun(b_run.run_id, downstream=True, actor=ALICE))
            await pynet.loop.sleep(POLL)
            gate.open()
            await marking
            await moving
            a_runs = await rerun_a
            b_runs = await rerun_b
            assert a_runs[0].run_id == first.run_id
            assert b_runs[0].run_id == b_run.run_id
            await h.advance(TWO_HOURS)

        h.run_engine(body)
        assert len(h.engine._run_locks) == 0
        for job in ("a", "b", "c"):
            assert h.run(job).state is RunState.SUCCEEDED
