"""Lay out the store's tables, and turn records into rows and back.

PyDB has no NULL yet: every column of every row must hold a value of its
type. So a value that may be missing (an attempt's ``ended_at`` while it
is still running, a run's ``held_from`` when it is not held) is kept in a
TEXT column, where empty text means "not known yet". A missing time is
written as ISO text, the same way PyDB writes its own TIMESTAMPs.

A ``Batch`` is a parcel of changes for one commit. The writer merges
many parcels into one before it goes to the post box.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import cast

from pydb.record import Value
from pydb.schema import Column, Schema
from pydb.types import DataType, from_iso, to_iso

from pyscheduler.engine.model import Attempt, Event, Outcome, Run, RunState, Trigger

RUNS = "runs"
ATTEMPTS = "attempts"
EVENTS = "events"
ENGINE = "engine"
CONFIGS = "configs"

T = DataType.TEXT
INT = DataType.INTEGER

TABLES: dict[str, Schema] = {
    RUNS: Schema(
        [
            Column("run_id", T, primary_key=True),
            Column("job", T, not_null=True),
            Column("run_date", DataType.DATE, not_null=True),
            Column("scheduled_at", DataType.TIMESTAMP, not_null=True),
            Column("state", T, not_null=True),
            Column("reason", T),
            Column("attempt", INT),
            Column("rerun", INT),
            Column("trigger", T),
            Column("config_version", INT),
            Column("created_at", DataType.TIMESTAMP),
            Column("updated_at", DataType.TIMESTAMP),
            Column("held_from", T),
        ]
    ),
    ATTEMPTS: Schema(
        [
            Column("run_id", T, not_null=True),
            Column("attempt", INT, not_null=True),
            Column("started_at", DataType.TIMESTAMP),
            Column("ended_at", T),
            Column("exit_code", T),
            Column("signal", T),
            Column("outcome", T),
            Column("pid", T),
            Column("stdout_path", T),
            Column("stderr_path", T),
            Column("status_path", T),
            Column("reason", T),
        ]
    ),
    EVENTS: Schema(
        [
            Column("at", DataType.TIMESTAMP, not_null=True),
            Column("actor", T),
            Column("run_id", T),
            Column("action", T, not_null=True),
            Column("detail", T),
        ]
    ),
    ENGINE: Schema([Column("key", T, primary_key=True), Column("value", T)]),
    CONFIGS: Schema(
        [
            Column("version", INT, primary_key=True),
            Column("loaded_at", DataType.TIMESTAMP),
            Column("digest", T),
            Column("sources_json", T),
        ]
    ),
}
"""Every table's schema, by table name."""

INDEXES: dict[str, list[tuple[str, str]]] = {
    RUNS: [("runs_job", "job"), ("runs_run_date", "run_date"), ("runs_state", "state")],
    ATTEMPTS: [("attempts_run_id", "run_id")],
    EVENTS: [("events_run_id", "run_id")],
}
"""The ``(index name, column)`` pairs of each table that has indexes."""

type Row = dict[str, Value]
type AttemptKey = tuple[str, int]


@dataclass(frozen=True)
class ConfigRecord:
    """Remember one loaded config: its version, when, a digest and the file texts."""

    version: int
    loaded_at: datetime
    digest: str
    sources_json: str


@dataclass
class Batch:
    """Collect the changes that go into one commit.

    Deletes are applied first, then the upserts, so a run deleted and
    then saved again in one batch ends up saved.
    """

    runs: list[Run] = field(default_factory=list[Run])
    attempts: list[Attempt] = field(default_factory=list[Attempt])
    events: list[Event] = field(default_factory=list[Event])
    engine: dict[str, str] = field(default_factory=dict[str, str])
    configs: list[ConfigRecord] = field(default_factory=list[ConfigRecord])
    deleted_run_ids: list[str] = field(default_factory=list[str])

    @property
    def is_empty(self) -> bool:
        """Return whether the batch changes nothing."""
        return not (
            self.runs
            or self.attempts
            or self.events
            or self.engine
            or self.configs
            or self.deleted_run_ids
        )

    def merge(self, other: Batch) -> None:
        """Fold a later batch into this one, the later change of a key winning.

        A delete in ``other`` drops this batch's pending upserts and events
        of that run, since they came earlier. ``other`` is not changed.
        """
        gone = set(other.deleted_run_ids)
        if gone:
            self.runs = [r for r in self.runs if r.run_id not in gone]
            self.attempts = [a for a in self.attempts if a.run_id not in gone]
            self.events = [e for e in self.events if e.run_id not in gone]
            known = set(self.deleted_run_ids)
            self.deleted_run_ids += [rid for rid in other.deleted_run_ids if rid not in known]
        runs = {r.run_id: r for r in self.runs}
        runs.update((r.run_id, r) for r in other.runs)
        self.runs = list(runs.values())
        attempts = {(a.run_id, a.attempt): a for a in self.attempts}
        attempts.update(((a.run_id, a.attempt), a) for a in other.attempts)
        self.attempts = list(attempts.values())
        configs = {c.version: c for c in self.configs}
        configs.update((c.version, c) for c in other.configs)
        self.configs = list(configs.values())
        self.events += other.events
        self.engine.update(other.engine)


def _opt_text(value: str | int | datetime | None) -> str:
    """Write an optional value as text, ``None`` becoming empty text."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_iso(value)
    return str(value)


def _opt_int(text: Value) -> int | None:
    """Read an optional whole number written by ``_opt_text``."""
    return int(cast(str, text)) if text else None


def _opt_time(text: Value) -> datetime | None:
    """Read an optional instant written by ``_opt_text``."""
    return cast(datetime, from_iso(cast(str, text), DataType.TIMESTAMP)) if text else None


def run_to_row(run: Run) -> Row:
    """Return the ``runs`` row for a run."""
    return {
        "run_id": run.run_id,
        "job": run.job,
        "run_date": run.run_date,
        "scheduled_at": run.scheduled_at,
        "state": run.state.value,
        "reason": run.reason,
        "attempt": run.attempt,
        "rerun": run.rerun,
        "trigger": run.trigger.value,
        "config_version": run.config_version,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "held_from": _opt_text(run.held_from),
    }


def run_from_row(row: Mapping[str, Value]) -> Run:
    """Return the run a ``runs`` row holds."""
    held = cast(str, row["held_from"])
    return Run(
        run_id=cast(str, row["run_id"]),
        job=cast(str, row["job"]),
        run_date=cast(date, row["run_date"]),
        scheduled_at=cast(datetime, row["scheduled_at"]),
        state=RunState(row["state"]),
        reason=cast(str, row["reason"]),
        attempt=cast(int, row["attempt"]),
        rerun=cast(int, row["rerun"]),
        trigger=Trigger(row["trigger"]),
        config_version=cast(int, row["config_version"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
        held_from=RunState(held) if held else None,
    )


def attempt_to_row(attempt: Attempt) -> Row:
    """Return the ``attempts`` row for an attempt."""
    return {
        "run_id": attempt.run_id,
        "attempt": attempt.attempt,
        "started_at": attempt.started_at,
        "ended_at": _opt_text(attempt.ended_at),
        "exit_code": _opt_text(attempt.exit_code),
        "signal": _opt_text(attempt.signal),
        "outcome": _opt_text(attempt.outcome),
        "pid": _opt_text(attempt.pid),
        "stdout_path": attempt.stdout_path,
        "stderr_path": attempt.stderr_path,
        "status_path": attempt.status_path,
        "reason": attempt.reason,
    }


def attempt_from_row(row: Mapping[str, Value]) -> Attempt:
    """Return the attempt an ``attempts`` row holds."""
    outcome = cast(str, row["outcome"])
    return Attempt(
        run_id=cast(str, row["run_id"]),
        attempt=cast(int, row["attempt"]),
        started_at=cast(datetime, row["started_at"]),
        ended_at=_opt_time(row["ended_at"]),
        exit_code=_opt_int(row["exit_code"]),
        signal=_opt_int(row["signal"]),
        outcome=Outcome(outcome) if outcome else None,
        pid=_opt_int(row["pid"]),
        stdout_path=cast(str, row["stdout_path"]),
        stderr_path=cast(str, row["stderr_path"]),
        status_path=cast(str, row["status_path"]),
        reason=cast(str, row["reason"]),
    )


def event_to_row(event: Event) -> Row:
    """Return the ``events`` row for an event."""
    return {
        "at": event.at,
        "actor": event.actor,
        "run_id": _opt_text(event.run_id),
        "action": event.action,
        "detail": event.detail,
    }


def event_from_row(row: Mapping[str, Value]) -> Event:
    """Return the event an ``events`` row holds."""
    run_id = cast(str, row["run_id"])
    return Event(
        at=cast(datetime, row["at"]),
        actor=cast(str, row["actor"]),
        run_id=run_id or None,
        action=cast(str, row["action"]),
        detail=cast(str, row["detail"]),
    )


def config_to_row(config: ConfigRecord) -> Row:
    """Return the ``configs`` row for a config record."""
    return {
        "version": config.version,
        "loaded_at": config.loaded_at,
        "digest": config.digest,
        "sources_json": config.sources_json,
    }


def config_from_row(row: Mapping[str, Value]) -> ConfigRecord:
    """Return the config record a ``configs`` row holds."""
    return ConfigRecord(
        version=cast(int, row["version"]),
        loaded_at=cast(datetime, row["loaded_at"]),
        digest=cast(str, row["digest"]),
        sources_json=cast(str, row["sources_json"]),
    )
