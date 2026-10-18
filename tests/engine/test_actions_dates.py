"""Test actions across run dates: self-offset jobs, old dates and dependency-only runs."""

from datetime import timedelta

from action_helpers import ALICE, actor_events
from engine_helpers import TODAY, MakeHarness

from pyscheduler.engine.model import RunState, Trigger

TEN_DAYS = timedelta(days=10)
TWENTY_DAYS = timedelta(days=20)
BACKFILLED = 11
TWO = 2
TUESDAY = TODAY + timedelta(days=1)

SELF_OFFSET = """
jobs:
  a:
    command: run-a
    schedule: "30 18 * * *"
    depends_on: [{job: a, run_date: -1}]
"""

DEP_ONLY = """
jobs:
  a:
    command: run-a
    schedule: manual
  d:
    command: run-d
    depends_on: [a]
"""


class TestSelfOffset:
    """A job that waits for its own previous run date never sends a walk on for ever."""

    def test_trigger_backfill_and_rerun(self, harness: MakeHarness) -> None:
        """Trigger today, backfill old dates, and rerun an evicted old run."""
        h = harness(SELF_OFFSET)

        async def body() -> None:
            """Use all three actions on the self-offset job."""
            today = await h.engine.trigger("a", actor=ALICE)
            assert today.state is RunState.WAITING
            start, end = TODAY - TWENTY_DAYS, TODAY - TEN_DAYS
            runs = await h.engine.backfill("a", start=start, end=end, actor=ALICE)
            assert len(runs) == BACKFILLED
            old = runs[-1]
            await h.engine.mark_success(old.run_id, actor=ALICE)
            await h.engine.settle()
            h.engine.book.evict_final_before(end + timedelta(days=1))
            assert h.engine.book.get(old.run_id) is None
            reopened = await h.engine.rerun(old.run_id, downstream=True, actor=ALICE)
            assert [run.run_id for run in reopened] == [old.run_id]
            await h.engine.settle()

        h.run_engine(body)

    def test_dependency_only_self_offset_makes_no_chain(self, harness: MakeHarness) -> None:
        """Make one run of a job needing its own yesterday, not one per future day."""
        h = harness(
            """
            jobs:
              x:
                command: run-x
                schedule: manual
              y:
                command: run-y
                depends_on: [x, {job: y, run_date: -1}]
            """
        )

        async def body() -> None:
            """Trigger ``x``."""
            await h.engine.trigger("x", actor=ALICE)
            await h.engine.settle()

        h.run_engine(body)
        assert len(h.runs("y")) == 1
        assert h.runs("y", TUESDAY) == []

    def test_offset_cycle_is_bounded(self, harness: MakeHarness) -> None:
        """Stop a dependency-only chain that cycles through an offset."""
        h = harness(
            """
            jobs:
              x:
                command: run-x
                schedule: manual
              p:
                command: run-p
                depends_on: [x, {job: q, run_date: -1}]
              q:
                command: run-q
                depends_on: [p]
            """
        )

        async def body() -> None:
            """Trigger ``x``."""
            await h.engine.trigger("x", actor=ALICE)
            await h.engine.settle()

        h.run_engine(body)
        assert len(h.runs("p")) == 1
        assert len(h.runs("q")) == 1


class TestDependencyOnlyRuns:
    """Who suppresses a dependency-only run, and the events of those made by actions."""

    def test_manual_run_does_not_suppress(self, harness: MakeHarness) -> None:
        """Make a dependency run beside a manual one, but not beside a backfilled one."""
        h = harness(DEP_ONLY)

        async def body() -> None:
            """Trigger ``d`` then ``a`` today; backfill ``d`` then trigger ``a`` tomorrow."""
            await h.engine.trigger("d", actor=ALICE)
            await h.engine.trigger("a", actor=ALICE)
            await h.engine.backfill("d", start=TUESDAY, end=TUESDAY, actor=ALICE)
            await h.engine.trigger("a", run_date=TUESDAY, actor=ALICE)
            await h.engine.settle()

        h.run_engine(body)
        assert [run.trigger for run in h.runs("d")] == [Trigger.MANUAL, Trigger.DEPENDENCY]
        assert [run.trigger for run in h.runs("d", TUESDAY)] == [Trigger.BACKFILL]

    def test_events_for_downstream_runs(self, harness: MakeHarness) -> None:
        """Log the dependency-only runs a trigger and a backfill create."""
        h = harness(DEP_ONLY)

        async def body() -> None:
            """Trigger ``a`` today and backfill it tomorrow."""
            made = await h.engine.trigger("a", actor=ALICE)
            filled = await h.engine.backfill("a", start=TUESDAY, end=TUESDAY, actor=ALICE)
            assert [run.job for run in (made, *filled)] == ["a", "a"]
            await h.engine.settle()

        h.run_engine(body)
        assert [e.action for e in actor_events(h, "d.2026-10-19.1")] == ["trigger"]
        assert [e.action for e in actor_events(h, "d.2026-10-20.1")] == ["backfill"]
        assert len(h.runs("a")) + len(h.runs("a", TUESDAY)) == TWO
