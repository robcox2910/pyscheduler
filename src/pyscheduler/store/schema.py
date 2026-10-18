"""Lay out the store's tables, and turn records into rows and back.

A value that may be missing (an attempt's ``ended_at`` while it is still
running, a run's ``held_from`` when it is not held) is stored as PyDB's
NULL, Python's ``None``: "we don't know yet". Every other column is
NOT NULL, so a half-filled row is refused instead of saved.

A ``Batch`` is a parcel of changes for one commit. The writer merges
many parcels into one before it goes to the post box.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import cast

from pydb.record import Value
from pydb.schema import Column, Schema
from pydb.types import DataType

from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import AlertKind, Criticality
from pyscheduler.engine.model import Attempt, Event, Outcome, Run, RunState, Trigger

RUNS = "runs"
ATTEMPTS = "attempts"
EVENTS = "events"
ENGINE = "engine"
CONFIGS = "configs"
ALERTS = "alerts"
DELIVERIES = "deliveries"
RUN_FILES = "run_files"

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
            Column("reason", T, not_null=True),
            Column("attempt", INT, not_null=True),
            Column("rerun", INT, not_null=True),
            Column("trigger", T, not_null=True),
            Column("config_version", INT, not_null=True),
            Column("created_at", DataType.TIMESTAMP, not_null=True),
            Column("updated_at", DataType.TIMESTAMP, not_null=True),
            Column("held_from", T),
        ]
    ),
    ATTEMPTS: Schema(
        [
            Column("run_id", T, not_null=True),
            Column("attempt", INT, not_null=True),
            Column("started_at", DataType.TIMESTAMP, not_null=True),
            Column("ended_at", DataType.TIMESTAMP),
            Column("exit_code", INT),
            Column("signal", INT),
            Column("outcome", T),
            Column("pid", INT),
            Column("stdout_path", T, not_null=True),
            Column("stderr_path", T, not_null=True),
            Column("status_path", T, not_null=True),
            Column("reason", T, not_null=True),
        ]
    ),
    EVENTS: Schema(
        [
            Column("at", DataType.TIMESTAMP, not_null=True),
            Column("actor", T, not_null=True),
            Column("run_id", T),
            Column("action", T, not_null=True),
            Column("detail", T, not_null=True),
        ]
    ),
    ENGINE: Schema([Column("key", T, primary_key=True), Column("value", T, not_null=True)]),
    CONFIGS: Schema(
        [
            Column("version", INT, primary_key=True),
            Column("loaded_at", DataType.TIMESTAMP, not_null=True),
            Column("digest", T, not_null=True),
            Column("sources_json", T, not_null=True),
        ]
    ),
    ALERTS: Schema(
        [
            Column("alert_id", T, primary_key=True),
            Column("kind", T, not_null=True),
            Column("run_id", T),
            Column("job", T),
            Column("run_date", DataType.DATE),
            Column("attempt", INT, not_null=True),
            Column("criticality", T, not_null=True),
            Column("raised_at", DataType.TIMESTAMP, not_null=True),
            Column("message", T, not_null=True),
            Column("detail", T, not_null=True),
        ]
    ),
    DELIVERIES: Schema(
        [
            Column("delivery_id", T, primary_key=True),
            Column("alert_id", T, not_null=True),
            Column("notifier", T, not_null=True),
            Column("status", T, not_null=True),
            Column("attempts", INT, not_null=True),
            Column("last_error", T, not_null=True),
            Column("updated_at", DataType.TIMESTAMP, not_null=True),
        ]
    ),
    RUN_FILES: Schema([Column("run_id", T, primary_key=True), Column("files", T, not_null=True)]),
}
"""Every table's schema, by table name."""

INDEXES: dict[str, list[tuple[str, str]]] = {
    RUNS: [("runs_job", "job"), ("runs_run_date", "run_date"), ("runs_state", "state")],
    ATTEMPTS: [("attempts_run_id", "run_id")],
    EVENTS: [("events_run_id", "run_id")],
    ALERTS: [("alerts_run_id", "run_id"), ("alerts_raised_at", "raised_at")],
    DELIVERIES: [("deliveries_alert_id", "alert_id"), ("deliveries_status", "status")],
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
    alerts: list[AlertRecord] = field(default_factory=list[AlertRecord])
    deliveries: list[DeliveryRecord] = field(default_factory=list[DeliveryRecord])
    run_files: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    deleted_alert_ids: list[str] = field(default_factory=list[str])

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
            or self.alerts
            or self.deliveries
            or self.run_files
            or self.deleted_alert_ids
        )

    def merge(self, other: Batch) -> None:
        """Fold a later batch into this one, the later change of a key winning.

        A delete in ``other`` drops this batch's pending upserts and events
        of that run, since they came earlier; so does a deleted alert id.
        ``other`` is not changed.
        """
        self._merge_alerts(other)
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

    def _merge_alerts(self, other: Batch) -> None:
        """Fold in ``other``'s alerts, deliveries and files, and its alert deletes."""
        gone_runs = set(other.deleted_run_ids)
        gone_alerts = set(other.deleted_alert_ids)
        gone_alerts |= {a.alert_id for a in self.alerts if a.run_id in gone_runs}
        if gone_alerts:
            self.alerts = [a for a in self.alerts if a.alert_id not in gone_alerts]
            self.deliveries = [d for d in self.deliveries if d.alert_id not in gone_alerts]
            known = set(self.deleted_alert_ids)
            self.deleted_alert_ids += [i for i in other.deleted_alert_ids if i not in known]
        self.run_files = {k: v for k, v in self.run_files.items() if k not in gone_runs}
        alerts = {a.alert_id: a for a in self.alerts}
        alerts.update((a.alert_id, a) for a in other.alerts)
        self.alerts = list(alerts.values())
        deliveries = {d.delivery_id: d for d in self.deliveries}
        deliveries.update((d.delivery_id, d) for d in other.deliveries)
        self.deliveries = list(deliveries.values())
        self.run_files.update(other.run_files)


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
        "held_from": run.held_from,
    }


def run_from_row(row: Mapping[str, Value]) -> Run:
    """Return the run a ``runs`` row holds."""
    held = row["held_from"]
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
        held_from=None if held is None else RunState(held),
    )


def attempt_to_row(attempt: Attempt) -> Row:
    """Return the ``attempts`` row for an attempt."""
    return {
        "run_id": attempt.run_id,
        "attempt": attempt.attempt,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "exit_code": attempt.exit_code,
        "signal": attempt.signal,
        "outcome": attempt.outcome,
        "pid": attempt.pid,
        "stdout_path": attempt.stdout_path,
        "stderr_path": attempt.stderr_path,
        "status_path": attempt.status_path,
        "reason": attempt.reason,
    }


def attempt_from_row(row: Mapping[str, Value]) -> Attempt:
    """Return the attempt an ``attempts`` row holds."""
    outcome = row["outcome"]
    return Attempt(
        run_id=cast(str, row["run_id"]),
        attempt=cast(int, row["attempt"]),
        started_at=cast(datetime, row["started_at"]),
        ended_at=cast(datetime | None, row["ended_at"]),
        exit_code=cast(int | None, row["exit_code"]),
        signal=cast(int | None, row["signal"]),
        outcome=None if outcome is None else Outcome(outcome),
        pid=cast(int | None, row["pid"]),
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
        "run_id": event.run_id,
        "action": event.action,
        "detail": event.detail,
    }


def event_from_row(row: Mapping[str, Value]) -> Event:
    """Return the event an ``events`` row holds."""
    return Event(
        at=cast(datetime, row["at"]),
        actor=cast(str, row["actor"]),
        run_id=cast(str | None, row["run_id"]),
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


def alert_to_row(alert: AlertRecord) -> Row:
    """Return the ``alerts`` row for an alert."""
    return {
        "alert_id": alert.alert_id,
        "kind": alert.kind.value,
        "run_id": alert.run_id,
        "job": alert.job,
        "run_date": alert.run_date,
        "attempt": alert.attempt,
        "criticality": alert.criticality.name,
        "raised_at": alert.raised_at,
        "message": alert.message,
        "detail": json.dumps(alert.detail),
    }


def alert_from_row(row: Mapping[str, Value]) -> AlertRecord:
    """Return the alert an ``alerts`` row holds."""
    return AlertRecord(
        alert_id=cast(str, row["alert_id"]),
        kind=AlertKind(row["kind"]),
        run_id=cast(str | None, row["run_id"]),
        job=cast(str | None, row["job"]),
        run_date=cast(date | None, row["run_date"]),
        attempt=cast(int, row["attempt"]),
        criticality=Criticality[cast(str, row["criticality"])],
        raised_at=cast(datetime, row["raised_at"]),
        message=cast(str, row["message"]),
        detail=json.loads(cast(str, row["detail"])),
    )


def delivery_to_row(delivery: DeliveryRecord) -> Row:
    """Return the ``deliveries`` row for a delivery."""
    return {
        "delivery_id": delivery.delivery_id,
        "alert_id": delivery.alert_id,
        "notifier": delivery.notifier,
        "status": delivery.status.value,
        "attempts": delivery.attempts,
        "last_error": delivery.last_error,
        "updated_at": delivery.updated_at,
    }


def delivery_from_row(row: Mapping[str, Value]) -> DeliveryRecord:
    """Return the delivery a ``deliveries`` row holds."""
    return DeliveryRecord(
        alert_id=cast(str, row["alert_id"]),
        notifier=cast(str, row["notifier"]),
        status=DeliveryStatus(row["status"]),
        attempts=cast(int, row["attempts"]),
        last_error=cast(str, row["last_error"]),
        updated_at=cast(datetime, row["updated_at"]),
    )
