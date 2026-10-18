"""When a run must have started, and what each alert about a run or attempt says.

``must_start_by`` is like a teacher taking the register: a clock time
(``"03:00"``) means three o'clock on the day the run *fired*, in the job's
time zone, and a duration (``30m``) counts from when the run was due. A
run that was made after its deadline had already passed (a catch-up, a
backfill of last month) is never marked late, or every backfill would
ring every bell at once.
"""

from datetime import UTC, datetime, timedelta

from pynet.messaging import JsonValue

from pyscheduler.config.model import JobDef
from pyscheduler.engine.model import Attempt, Run
from pyscheduler.values import format_duration

type Wording = tuple[str, dict[str, JsonValue]]
"""An alert's message and its detail."""


def start_deadline(run: Run, job: JobDef) -> datetime | None:
    """Return when ``run`` must have started by (ruling P2-R2), or ``None`` for no deadline.

    Only a run's first life (``attempt == 0``) has one, and only if the
    run was created before it.
    """
    rule = job.must_start_by
    if rule is None or run.attempt != 0:
        return None
    if isinstance(rule, timedelta):
        deadline = run.scheduled_at + rule
    else:
        day = run.scheduled_at.astimezone(job.timezone).date()
        deadline = datetime.combine(day, rule, tzinfo=job.timezone).astimezone(UTC)
    return deadline if run.created_at < deadline else None


def _seconds(value: timedelta) -> float:
    """Return a duration in seconds."""
    return value.total_seconds()


def failed(run: Run) -> Wording:
    """Word the alert for a run that ended failed."""
    return f"{run.run_id} failed: {run.reason}", {"attempt": run.attempt, "reason": run.reason}


def retrying(run: Run) -> Wording:
    """Word the alert for a run waiting to try again."""
    detail: dict[str, JsonValue] = {"attempt": run.attempt, "reason": run.reason}
    return f"{run.run_id} failed and will try again ({run.reason})", detail


def blocked(run: Run) -> Wording:
    """Word the alert for a run that cannot start because an upstream failed."""
    return f"{run.run_id} cannot start: {run.reason}", {"reason": run.reason}


def lost(attempt: Attempt) -> Wording:
    """Word the alert for an attempt whose process was lost."""
    detail: dict[str, JsonValue] = {
        "attempt": attempt.attempt,
        "started_at": attempt.started_at.astimezone(UTC).isoformat(),
    }
    message = f"{attempt.run_id} attempt {attempt.attempt} was lost: nobody saw it end"
    return message, detail


def too_short(attempt: Attempt, runtime: timedelta, minimum: timedelta) -> Wording:
    """Word the alert for an attempt that succeeded suspiciously quickly."""
    detail: dict[str, JsonValue] = {
        "runtime_seconds": _seconds(runtime),
        "min_runtime_seconds": _seconds(minimum),
    }
    message = (
        f"{attempt.run_id} attempt {attempt.attempt} succeeded after only "
        f"{format_duration(runtime)} (min_runtime {format_duration(minimum)})"
    )
    return message, detail


def too_long(attempt: Attempt, runtime: timedelta, maximum: timedelta) -> Wording:
    """Word the alert for an attempt still running past its ``max_runtime``."""
    detail: dict[str, JsonValue] = {
        "runtime_seconds": _seconds(runtime),
        "max_runtime_seconds": _seconds(maximum),
    }
    message = (
        f"{attempt.run_id} attempt {attempt.attempt} has been running for "
        f"{format_duration(runtime)} (max_runtime {format_duration(maximum)})"
    )
    return message, detail


def never_started(run: Run, deadline: datetime, job: JobDef) -> Wording:
    """Word the alert for a run not started by its ``must_start_by`` deadline."""
    local = deadline.astimezone(job.timezone)
    detail: dict[str, JsonValue] = {
        "deadline": deadline.astimezone(UTC).isoformat(),
        "state": run.state.value,
    }
    return f"{run.run_id} has not started by {local:%H:%M %Z}", detail
