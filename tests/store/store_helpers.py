"""Build runs and attempts for the store tests."""

from datetime import UTC, date, datetime, timedelta

from pyscheduler.engine.model import Attempt, Outcome, Run, RunState, Trigger, make_run_id

DAY = date(2026, 10, 17)
T0 = datetime(2026, 10, 17, 9, tzinfo=UTC)


def make_run(
    job: str = "extract",
    run_date: date = DAY,
    seq: int = 1,
    state: RunState = RunState.SCHEDULED,
    *,
    scheduled_at: datetime = T0,
    held_from: RunState | None = None,
) -> Run:
    """Return a run with sensible defaults."""
    return Run(
        run_id=make_run_id(job, run_date, seq),
        job=job,
        run_date=run_date,
        scheduled_at=scheduled_at,
        state=state,
        reason="",
        attempt=0,
        rerun=0,
        trigger=Trigger.SCHEDULE,
        config_version=1,
        created_at=T0,
        updated_at=T0,
        held_from=held_from,
    )


def make_attempt(
    run_id: str,
    attempt: int = 1,
    *,
    started_at: datetime = T0,
    took: timedelta | None = None,
    outcome: Outcome | None = None,
) -> Attempt:
    """Return an attempt; with ``took`` it has ended that long after it started."""
    ended = None if took is None else started_at + took
    return Attempt(
        run_id=run_id,
        attempt=attempt,
        started_at=started_at,
        ended_at=ended,
        exit_code=None if ended is None else 0,
        signal=None,
        outcome=outcome,
        pid=None if ended is None else 4321,
        stdout_path=f"/logs/{run_id}.{attempt}.out",
        stderr_path=f"/logs/{run_id}.{attempt}.err",
        status_path=f"/state/{run_id}.{attempt}.status",
        reason="",
    )
