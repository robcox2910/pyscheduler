"""Tests for the run model: states, run ids and the frozen records."""

import dataclasses
from datetime import UTC, date, datetime

import pytest

from pyscheduler.engine.model import (
    FINAL_STATES,
    Run,
    RunState,
    Trigger,
    make_run_id,
    parse_run_id,
)

DAY = date(2026, 10, 17)
SEQ = 3
NOW = datetime(2026, 10, 17, 9, tzinfo=UTC)


class TestRunIds:
    """Run ids are ``<job>.<run_date>.<seq>`` and parse back exactly."""

    def test_run_id_roundtrip(self) -> None:
        """Make an id and parse it back to the same parts."""
        run_id = make_run_id("a-b", DAY, SEQ)
        assert run_id == "a-b.2026-10-17.3"
        assert parse_run_id(run_id) == ("a-b", DAY, SEQ)

    def test_underscore_job(self) -> None:
        """Keep underscores and dashes in the job part."""
        assert parse_run_id("load_x-y.2026-10-17.12") == ("load_x-y", DAY, 12)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "job",
            "job.2026-10-17",
            ".2026-10-17.1",
            "a.b.2026-10-17.1",
            "job.2026-13-01.1",
            "job.20261017.1",
            "job.2026-10-17.x",
            "job.2026-10-17.-1",
            "job.2026-10-17.",
            "job.2026-W42-6.1",
            "job.2026-10-17.01",
            "job.2026-10-17.0",
            "job. 2026-10-17.1",
        ],
    )
    def test_malformed_ids_raise(self, bad: str) -> None:
        """Reject ids that are not job, ISO date and a whole number."""
        with pytest.raises(ValueError, match="malformed run id"):
            parse_run_id(bad)


class TestStates:
    """Final states and frozen runs."""

    def test_final_states(self) -> None:
        """List exactly the four final states."""
        assert {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.SKIPPED,
            RunState.CANCELLED,
        } == FINAL_STATES
        assert RunState.RUNNING not in FINAL_STATES

    def test_run_is_frozen_and_replaceable(self) -> None:
        """Change a run only by making a changed copy."""
        run = Run(
            run_id=make_run_id("j", DAY, 1),
            job="j",
            run_date=DAY,
            scheduled_at=NOW,
            state=RunState.SCHEDULED,
            reason="",
            attempt=0,
            rerun=0,
            trigger=Trigger.SCHEDULE,
            config_version=1,
            created_at=NOW,
            updated_at=NOW,
            held_from=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            run.state = RunState.RUNNING  # type: ignore[misc]
        done = dataclasses.replace(run, state=RunState.SUCCEEDED)
        assert done.is_final
        assert not run.is_final
