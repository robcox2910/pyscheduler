"""Test force-start, skip, mark and the rerun downstream search."""

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
    TWO_ATTEMPTS,
    launched,
    status_of,
)
from engine_helpers import MakeHarness

from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import RunState
from pyscheduler.testing import FakeResult


class TestForceStart:
    """Force-start skips dependencies and overlap but not pools or the cap."""

    def test_force_start_ignores_deps_not_pools(self, harness: MakeHarness) -> None:
        """Queue a waiting run at once, but start it only when the pool frees."""
        h = harness(
            """
            pools: {db: 1}
            jobs:
              up:
                command: run-up
                schedule: manual
              hog:
                command: run-hog
                schedule: manual
                pools: {db: 1}
              b:
                command: run-b
                schedule: manual
                pools: {db: 1}
                depends_on: [up]
            """
        )
        h.launcher.plan("hog", FakeResult(duration=TEN_MINUTES))

        async def body() -> None:
            """Fill the pool, trigger ``b`` (waits on ``up``), force it."""
            await h.engine.trigger("hog", actor=ALICE)
            b = await h.engine.trigger("b", actor=ALICE)
            assert b.state is RunState.WAITING
            forced = await h.engine.force_start(b.run_id, actor=ALICE)
            assert forced.state is RunState.QUEUED
            assert forced.reason == "force-started by alice"
            await h.engine.settle()
            assert launched(h) == ["hog.2026-10-19.1"]
            await h.advance(TEN_MINUTES)
            assert launched(h) == ["hog.2026-10-19.1", "b.2026-10-19.1"]
            await h.advance(ONE_MINUTE)

        h.run_engine(body)
        assert h.run("b").state is RunState.SUCCEEDED

    def test_force_start_ignores_overlap(self, harness: MakeHarness) -> None:
        """Start a run waiting for its job's previous run straight away."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Run ``m`` once, trigger it again (waits), force the second."""
            await h.engine.trigger("m", actor=ALICE)
            second = await h.engine.trigger("m", actor=ALICE)
            assert second.state is RunState.WAITING
            await h.engine.force_start(second.run_id, actor=ALICE)
            await h.engine.settle()
            assert len(h.launcher.running) == TWO_ATTEMPTS

        h.run_engine(body)

    def test_force_start_running_409(self, harness: MakeHarness) -> None:
        """Refuse to force-start a running run."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Start ``m`` and force it."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            with pytest.raises(ActionError) as caught:
                await h.engine.force_start(run.run_id, actor=ALICE)
            assert status_of(caught) == CONFLICT

        h.run_engine(body)


class TestEndByHand:
    """Skip and mark set a final state by hand and re-check the downstream."""

    def test_skip_satisfies_success_dep(self, harness: MakeHarness) -> None:
        """Run ``b`` once its failed (retry-waiting) upstream ``a`` is skipped."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(exit_code=1))

        async def body() -> None:
            """Let ``a`` fail into its retry wait, then skip it."""
            await h.advance_to(AT_1830 + ONE_MINUTE)
            assert h.run("a").state is RunState.RETRY_WAIT
            skipped = await h.engine.skip("a.2026-10-19.1", actor=ALICE)
            assert skipped.state is RunState.SKIPPED
            assert skipped.reason == "skip by alice"
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("b").state is RunState.SUCCEEDED
        assert len(h.launcher.launched) == TWO_ATTEMPTS

    def test_skip_final_409(self, harness: MakeHarness) -> None:
        """Refuse to skip a run that is already final."""
        h = harness(CHAIN)

        async def body() -> None:
            """Let ``a`` succeed and skip it."""
            await h.advance_to(AT_1830 + ONE_MINUTE)
            with pytest.raises(ActionError) as caught:
                await h.engine.skip("a.2026-10-19.1", actor=ALICE)
            assert status_of(caught) == CONFLICT

        h.run_engine(body)

    def test_mark_failed_blocks_downstream(self, harness: MakeHarness) -> None:
        """Block ``b`` when its retry-waiting upstream is marked failed."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(exit_code=1))

        async def body() -> None:
            """Let ``a`` fail into its retry wait, then mark it failed."""
            await h.advance_to(AT_1830 + ONE_MINUTE)
            marked = await h.engine.mark_failed("a.2026-10-19.1", actor=ALICE)
            assert marked.state is RunState.FAILED
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("b").state is RunState.BLOCKED
        assert len(h.launcher.launched) == 1

    def test_mark_success_on_final_unblocks(self, harness: MakeHarness) -> None:
        """Unblock ``b`` by marking its failed upstream succeeded."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(exit_code=1))

        async def body() -> None:
            """Let ``a`` fail for good, then mark it succeeded."""
            await h.advance_to(AT_1830 + TEN_MINUTES)
            assert h.run("b").state is RunState.BLOCKED
            await h.engine.mark_success("a.2026-10-19.1", actor=ALICE)
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("a").reason == "mark_success by alice"
        assert h.run("b").state is RunState.SUCCEEDED

    def test_mark_running_409(self, harness: MakeHarness) -> None:
        """Refuse to mark a running run."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Start ``m`` and mark it."""
            run = await h.engine.trigger("m", actor=ALICE)
            await h.engine.settle()
            with pytest.raises(ActionError) as caught:
                await h.engine.mark_success(run.run_id, actor=ALICE)
            assert status_of(caught) == CONFLICT

        h.run_engine(body)


class TestDownstreamSearch:
    """Rerun's downstream search follows only matching, enabled dependencies."""

    def test_rerun_downstream_skips_disabled_and_other_deps(self, harness: MakeHarness) -> None:
        """Reopen ``b`` and ``c`` (through both of its dependencies) but not disabled ``e``."""
        h = harness(
            """
            jobs:
              a:
                command: run-a
                schedule: manual
              b:
                command: run-b
                depends_on: [a]
              c:
                command: run-c
                depends_on: [b, a]
              e:
                command: run-e
                depends_on: [a]
                enabled: false
            """
        )

        async def body() -> None:
            """Run the chain, then rerun ``a`` with its downstream."""
            run = await h.engine.trigger("a", actor=ALICE)
            await h.advance(TEN_MINUTES)
            assert h.run("c").state is RunState.SUCCEEDED
            runs = await h.engine.rerun(run.run_id, downstream=True, actor=ALICE)
            assert sorted(r.job for r in runs) == ["a", "b", "c"]
            await h.advance(TEN_MINUTES)

        h.run_engine(body)
        assert h.run("c").attempt == TWO_ATTEMPTS
