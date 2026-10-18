"""Describe runs, attempts and events: the facts the engine writes down.

A **run** is one go of a job for one run date, like one lesson on the
timetable. An **attempt** is one try at that run: a run that fails and
retries has several attempts. An **event** is a line in the logbook:
who did what, and when.

All three are frozen dataclasses. The engine never edits one in place;
it makes a changed copy with ``dataclasses.replace`` and sends the copy
to the store, so a value it already handed out never changes under
anyone's feet.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

RUN_ID_PARTS = 3
"""A run id has three dot-separated parts: job, run date and sequence."""


class RunState(StrEnum):
    """Name the traffic-light states a run moves through."""

    SCHEDULED = "scheduled"
    WAITING = "waiting"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    HELD = "held"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


FINAL_STATES = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.SKIPPED, RunState.CANCELLED}
)
"""The states a run never leaves on its own (only an operator's rerun reopens it)."""


class Trigger(StrEnum):
    """Name what caused a run to be created."""

    SCHEDULE = "schedule"
    DEPENDENCY = "dependency"
    MANUAL = "manual"
    BACKFILL = "backfill"
    CATCH_UP = "catch_up"


class Outcome(StrEnum):
    """Name how an attempt ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    KILLED = "killed"
    LOST = "lost"
    START_ERROR = "start_error"


@dataclass(frozen=True)
class Run:
    """Record one run of a job for one run date.

    Attributes:
        run_id: ``<job>.<run_date>.<seq>``, made by ``make_run_id``.
        job: The job's name.
        run_date: The business date the run is for.
        scheduled_at: The aware UTC instant the run was due.
        state: Where the run is in its lifecycle.
        reason: A short human explanation of the latest state change.
        attempt: The number of the latest attempt (0 before the first).
        rerun: How many times an operator has reopened the run.
        trigger: What created the run.
        config_version: The config version whose job definition it uses.
        created_at: When the run was created.
        updated_at: When the run last changed.
        held_from: The state the run was in when it was held, else ``None``.

    """

    run_id: str
    job: str
    run_date: date
    scheduled_at: datetime
    state: RunState
    reason: str
    attempt: int
    rerun: int
    trigger: Trigger
    config_version: int
    created_at: datetime
    updated_at: datetime
    held_from: RunState | None = None

    @property
    def is_final(self) -> bool:
        """Return whether the run is in a final state."""
        return self.state in FINAL_STATES


@dataclass(frozen=True)
class Attempt:
    """Record one try at running a run's command.

    Attributes:
        run_id: The run this attempt belongs to.
        attempt: The attempt number, starting at 1.
        started_at: When the attempt started.
        ended_at: When it ended, or ``None`` while it runs.
        exit_code: The command's exit code, if it exited.
        signal: The signal that killed it, if one did.
        outcome: How the attempt ended, or ``None`` while it runs.
        pid: The minder's process id, once started.
        stdout_path: Where the command's standard output goes.
        stderr_path: Where the command's standard error goes.
        status_path: Where the minder writes its status note.
        reason: A short human explanation of the outcome.

    """

    run_id: str
    attempt: int
    started_at: datetime
    ended_at: datetime | None
    exit_code: int | None
    signal: int | None
    outcome: Outcome | None
    pid: int | None
    stdout_path: str
    stderr_path: str
    status_path: str
    reason: str


@dataclass(frozen=True)
class Event:
    """Record one line in the scheduler's logbook.

    Attributes:
        at: When it happened.
        actor: Who did it (``engine``, or an operator's token name).
        run_id: The run it concerns, or ``None`` for scheduler-wide events.
        action: What happened, such as ``hold`` or ``config_loaded``.
        detail: Extra facts, as JSON text.

    """

    at: datetime
    actor: str
    run_id: str | None
    action: str
    detail: str


def make_run_id(job: str, run_date: date, seq: int) -> str:
    """Return the run id for the ``seq``-th run of ``job`` on ``run_date``."""
    return f"{job}.{run_date.isoformat()}.{seq}"


def parse_run_id(run_id: str) -> tuple[str, date, int]:
    """Split a run id back into its job, run date and sequence number.

    Job names may contain ``-`` and ``_`` but never ``.``, so the id is
    split on its dots.

    Raises:
        ValueError: If the id is not ``<job>.<YYYY-MM-DD>.<seq>``.

    """
    parts = run_id.rsplit(".", RUN_ID_PARTS - 1)
    if len(parts) != RUN_ID_PARTS or not parts[0] or "." in parts[0]:
        msg = f"malformed run id {run_id!r}"
        raise ValueError(msg)
    job, day, seq_text = parts
    if not seq_text.isdigit() or not seq_text.isascii():
        msg = f"malformed run id {run_id!r}: bad sequence {seq_text!r}"
        raise ValueError(msg)
    try:
        run_date = date.fromisoformat(day)
    except ValueError as exc:
        msg = f"malformed run id {run_id!r}: bad date {day!r}"
        raise ValueError(msg) from exc
    if len(day) != len("YYYY-MM-DD"):
        msg = f"malformed run id {run_id!r}: bad date {day!r}"
        raise ValueError(msg)
    return job, run_date, int(seq_text)
