"""Test trigger, backfill, failed action commits and the action logbook."""

import json
from datetime import date, timedelta

import pytest
from action_helpers import (
    ALICE,
    BAD_REQUEST,
    CONFLICT,
    DISK_FULL,
    FOUR_HOURS,
    FRIDAY,
    MANUAL,
    NOT_FOUND,
    TUESDAY,
    UNAVAILABLE,
    actor_events,
    status_of,
)
from engine_helpers import START, TODAY, MakeHarness

from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import RunState, Trigger
from pyscheduler.store import Batch
from pyscheduler.testing import FakeResult


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
