"""Test operator actions: trigger, rerun, hold, kill, force-start, skip, mark and backfill."""

import json
from datetime import date, timedelta

import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness

from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import Event, RunState, Trigger
from pyscheduler.store import Batch
from pyscheduler.testing import FakeResult

ONE_MINUTE = timedelta(minutes=1)
TEN_MINUTES = timedelta(minutes=10)
FOUR_HOURS = timedelta(hours=4)
AT_1830 = START.replace(hour=18, minute=30)
ALICE = "alice"
DISK_FULL = "disk full"
NOT_FOUND = 404
CONFLICT = 409
BAD_REQUEST = 400
UNAVAILABLE = 503
TWO_ATTEMPTS = 2
THREE_ATTEMPTS = 3
FRIDAY = date(2026, 10, 16)
TUESDAY = date(2026, 10, 20)

CHAIN = """
jobs:
  a:
    command: run-a
    schedule: "30 18 * * *"
    retries: {count: 1, delay: 1m}
  b:
    command: run-b
    depends_on: [a]
"""

MANUAL = """
jobs:
  m:
    command: run-m
    schedule: manual
    retries: {count: 3, delay: 10m}
  n:
    command: run-n
    schedule: manual
    enabled: false
"""


def launched(h: Harness) -> list[str]:
    """Return the run ids launched so far, in order."""
    return [spec.run_id for spec in h.launcher.launched]


def actor_events(h: Harness, run_id: str) -> list[Event]:
    """Return the events of ``run_id`` that ``ALICE`` caused."""
    with h.reopened() as store:
        return [event for event in store.events_for(run_id) if event.actor == ALICE]


def status_of(error: pytest.ExceptionInfo[ActionError]) -> int:
    """Return the HTTP-style status of a caught action error."""
    return error.value.status


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


class TestTrigger:
    """Trigger creates a manual run now."""

    def test_trigger_defaults_and_errors(self, harness: MakeHarness) -> None:
        """Make a manual run for today; refuse unknown (404) and disabled (409) jobs."""
        h = harness(MANUAL)

        async def body() -> None:
            """Trigger ``m`` twice and the others once."""
            first = await h.engine.trigger("m", actor=ALICE)
            second = await h.engine.trigger("m", run_date=TUESDAY, actor=ALICE)
            assert first.run_id == "m.2026-10-19.1"
            assert first.trigger is Trigger.MANUAL
            assert first.scheduled_at == START
            assert second.run_date == TUESDAY
            with pytest.raises(ActionError) as caught:
                await h.engine.trigger("nope", actor=ALICE)
            assert status_of(caught) == NOT_FOUND
            with pytest.raises(ActionError) as caught:
                await h.engine.trigger("n", actor=ALICE)
            assert status_of(caught) == CONFLICT
            await h.engine.settle()

        h.run_engine(body)

    def test_unknown_run_404(self, harness: MakeHarness) -> None:
        """Answer 404 for a run id that names no run, well formed or not."""
        h = harness(MANUAL)

        async def body() -> None:
            """Act on missing runs."""
            for run_id in ("m.2026-10-19.7", "not a run id"):
                with pytest.raises(ActionError) as caught:
                    await h.engine.hold(run_id, actor=ALICE)
                assert status_of(caught) == NOT_FOUND

        h.run_engine(body)

    def test_stopping_engine_503(self, harness: MakeHarness) -> None:
        """Refuse every action once the engine is stopping or failed."""
        h = harness(MANUAL)

        async def body() -> None:
            """Stop the engine and try an action."""
            await h.engine.stop()
            with pytest.raises(ActionError) as caught:
                await h.engine.trigger("m", actor=ALICE)
            assert status_of(caught) == UNAVAILABLE
            assert "stopping" in str(caught.value)
            h.engine.error = OSError("disk full")
            with pytest.raises(ActionError) as caught:
                await h.engine.trigger("m", actor=ALICE)
            assert "scheduler failed" in str(caught.value)

        h.run_engine(body)


class TestBackfill:
    """Backfill creates the missing runs of a date range."""

    BUSINESS = """
    calendars:
      uk-business:
        weekdays: [mon, tue, wed, thu, fri]
    jobs:
      load:
        command: run-load
        schedule: ["0 6 * * *", "30 18 * * *"]
        calendar: uk-business
      report:
        command: run-report
        depends_on: [load]
    """

    def test_backfill_three_business_days(self, harness: MakeHarness) -> None:
        """Fill Fri and Tue (Mon already ran), skipping the weekend, with downstream runs."""
        h = harness(self.BUSINESS)

        async def body() -> None:
            """Let Monday's first firing happen, then backfill Fri to Tue."""
            await h.advance_to(START.replace(hour=7))
            assert len(h.runs("load")) == 1
            runs = await h.engine.backfill("load", start=FRIDAY, end=TUESDAY, actor=ALICE)
            assert [run.run_date for run in runs] == [FRIDAY, TUESDAY]
            assert all(run.trigger is Trigger.BACKFILL for run in runs)
            assert runs[0].scheduled_at == START.replace(day=16, hour=6)
            await h.engine.settle()

        h.run_engine(body)
        for day in (FRIDAY, TODAY, TUESDAY):
            assert len(h.runs("load", day)) == 1
            assert h.runs("report", day)[0].trigger is Trigger.DEPENDENCY
        assert h.runs("load", date(2026, 10, 17)) == []
        events = actor_events(h, "load.2026-10-16.1")
        assert [event.action for event in events] == ["backfill"]

    def test_backfill_dependency_only_and_errors(self, harness: MakeHarness) -> None:
        """Start a dependency-only backfill at midnight; refuse bad ranges and jobs."""
        h = harness(self.BUSINESS)

        async def body() -> None:
            """Backfill ``report`` for Friday, then try bad requests."""
            runs = await h.engine.backfill("report", start=FRIDAY, end=FRIDAY, actor=ALICE)
            assert runs[0].scheduled_at == START.replace(day=16)
            bad = [
                ("load", TUESDAY, FRIDAY, BAD_REQUEST),
                ("load", FRIDAY, FRIDAY + timedelta(days=366), BAD_REQUEST),
                ("nope", FRIDAY, FRIDAY, NOT_FOUND),
            ]
            for job, start, end, status in bad:
                with pytest.raises(ActionError) as caught:
                    await h.engine.backfill(job, start=start, end=end, actor=ALICE)
                assert status_of(caught) == status
            await h.engine.settle()

        h.run_engine(body)

    def test_backfill_previous_business_day_and_weekly(self, harness: MakeHarness) -> None:
        """Use the next business day's firing, and pass over dates that never fire."""
        h = harness(
            """
            calendars:
              uk-business:
                weekdays: [mon, tue, wed, thu, fri]
            jobs:
              eod:
                command: run-eod
                schedule: "0 2 * * *"
                calendar: uk-business
                run_date: previous_business_day
              weekly:
                command: run-weekly
                schedule: "0 9 * * fri"
                calendar: uk-business
            """
        )

        async def body() -> None:
            """Backfill both over Fri to Mon."""
            eod = await h.engine.backfill("eod", start=FRIDAY, end=FRIDAY, actor=ALICE)
            weekly = await h.engine.backfill("weekly", start=FRIDAY, end=TODAY, actor=ALICE)
            assert [run.scheduled_at for run in eod] == [START.replace(hour=2)]
            assert [run.run_date for run in weekly] == [FRIDAY]
            await h.engine.settle()

        h.run_engine(body)


class TestCommitFailure:
    """An action whose commit fails reports 503 and leaves the engine failed."""

    def test_action_commit_fails_503(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answer 503 when the action's own commit fails."""
        h = harness(MANUAL)
        real = h.store.apply

        def apply(batch: Batch) -> None:
            """Fail any commit carrying an operator's event."""
            if any(event.actor == ALICE for event in batch.events):
                raise OSError(DISK_FULL)
            real(batch)

        monkeypatch.setattr(h.store, "apply", apply)

        async def body() -> None:
            """Trigger, and see the failure."""
            with pytest.raises(ActionError) as caught:
                await h.engine.trigger("m", actor=ALICE)
            assert status_of(caught) == UNAVAILABLE
            assert DISK_FULL in str(caught.value)

        h.run_engine(body)
        assert h.launcher.launched == []
        # Memory holds only what is durable (a later group may have made a
        # pumped ``running`` durable; recovery would call it lost).
        for run in h.runs("m"):
            assert h.engine.book.published(run.run_id) == run


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


class TestEvents:
    """Every action leaves one logbook line naming who did it."""

    def test_events_recorded(self, harness: MakeHarness) -> None:
        """Record trigger, hold, release, force-start, kill, rerun, mark and skip."""
        h = harness(MANUAL)
        h.launcher.plan("m", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Walk three runs of ``m`` through every action but backfill."""
            run = await h.engine.trigger("m", actor=ALICE)
            other = await h.engine.trigger("m", actor=ALICE)
            third = await h.engine.trigger("m", run_date=TUESDAY, actor=ALICE)
            await h.engine.settle()
            await h.engine.hold(other.run_id, actor=ALICE)
            released = await h.engine.release(other.run_id, actor=ALICE)
            assert released.state is RunState.WAITING
            await h.engine.hold(other.run_id, actor=ALICE)
            await h.engine.force_start(other.run_id, actor=ALICE)
            await h.engine.hold(third.run_id, actor=ALICE)
            await h.engine.skip(third.run_id, actor=ALICE)
            await h.engine.settle()
            await h.engine.kill(other.run_id, actor=ALICE)
            await h.engine.kill(run.run_id, actor=ALICE)
            await h.engine.settle()
            await h.engine.rerun(run.run_id, actor=ALICE)
            await h.engine.settle()
            await h.engine.kill(run.run_id, actor=ALICE)
            await h.engine.settle()
            await h.engine.mark_success(run.run_id, actor=ALICE)

        h.run_engine(body)
        first = actor_events(h, "m.2026-10-19.1")
        assert [e.action for e in first] == ["trigger", "kill", "rerun", "kill", "mark_success"]
        second = actor_events(h, "m.2026-10-19.2")
        assert [e.action for e in second] == [
            "trigger",
            "hold",
            "release",
            "hold",
            "force_start",
            "kill",
        ]
        third = actor_events(h, "m.2026-10-20.1")
        assert [e.action for e in third] == ["trigger", "hold", "skip"]
        detail = json.loads(second[1].detail)
        assert detail == {"before": "waiting", "after": "held"}
        assert json.loads(first[0].detail)["before"] is None
