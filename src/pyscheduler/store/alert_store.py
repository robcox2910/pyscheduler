"""The alert side of the store: alerts, their deliveries and matched files.

Alerts and deliveries are upserted by primary key, so writing the same
alert twice is harmless. ``AlertTables`` keeps its own address books
(id to PyDB record id) and, like the main store, changes them only after
a commit has succeeded. The ``Store`` calls it from inside its own
transaction and holds the database lock around every call.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from pydb.database import Database
from pydb.record import Record, Value
from pydb.table import Table

from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import AlertKind
from pyscheduler.store.schema import (
    ALERTS,
    DELIVERIES,
    RUN_FILES,
    Batch,
    alert_from_row,
    alert_to_row,
    delivery_from_row,
    delivery_to_row,
)
from pyscheduler.store.staging import Staged, upsert

EARLIEST = datetime.min.replace(tzinfo=UTC)
LATEST = datetime.max.replace(tzinfo=UTC)


@dataclass
class AlertStage:
    """Hold the address-book changes of one commit's alert writes."""

    alerts: Staged[str]
    deliveries: Staged[str]
    files: Staged[str]
    _all: tuple[Staged[str], ...] = field(init=False)

    def __post_init__(self) -> None:
        """Remember all three books, to publish them together."""
        self._all = (self.alerts, self.deliveries, self.files)

    def publish(self) -> None:
        """Copy the changes into the real address books."""
        for staged in self._all:
            staged.publish()


class AlertTables:
    """Read and write the ``alerts``, ``deliveries`` and ``run_files`` tables."""

    def __init__(self, db: Database) -> None:
        """Wrap the three tables and read their address books."""
        self._alerts = db.get_table(ALERTS)
        self._deliveries = db.get_table(DELIVERIES)
        self._files = db.get_table(RUN_FILES)
        self._alert_ids = {cast(str, r["alert_id"]): r.record_id for r in self._alerts.select()}
        self._delivery_ids = {
            cast(str, r["delivery_id"]): r.record_id for r in self._deliveries.select()
        }
        self._file_ids = {cast(str, r["run_id"]): r.record_id for r in self._files.select()}

    # Writing (inside the store's transaction) ---------------------------

    def stage(self) -> AlertStage:
        """Start the address-book changes of one commit."""
        return AlertStage(
            Staged(self._alert_ids), Staged(self._delivery_ids), Staged(self._file_ids)
        )

    def delete_run(self, stage: AlertStage, run_id: str) -> None:
        """Delete a run's alerts, their deliveries and its matched files."""
        for record in _by_index(self._alerts, "alerts_run_id", run_id):
            self.delete_alert(stage, cast(str, record["alert_id"]))
        record_id = stage.files.get(run_id)
        if record_id is not None:
            self._files.delete(record_id)
            stage.files.put(run_id, None)

    def delete_alert(self, stage: AlertStage, alert_id: str) -> None:
        """Delete one alert with its deliveries."""
        for record in _by_index(self._deliveries, "deliveries_alert_id", alert_id):
            self._deliveries.delete(record.record_id)
            stage.deliveries.put(cast(str, record["delivery_id"]), None)
        record_id = stage.alerts.get(alert_id)
        if record_id is not None:
            self._alerts.delete(record_id)
            stage.alerts.put(alert_id, None)

    def write(self, stage: AlertStage, batch: Batch) -> None:
        """Upsert the batch's alerts, deliveries and matched files."""
        for alert in batch.alerts:
            upsert(self._alerts, stage.alerts, alert.alert_id, alert_to_row(alert))
        for delivery in batch.deliveries:
            row = delivery_to_row(delivery)
            upsert(self._deliveries, stage.deliveries, delivery.delivery_id, row)
        for run_id, files in batch.run_files.items():
            row: dict[str, Value] = {"run_id": run_id, "files": json.dumps(list(files))}
            upsert(self._files, stage.files, run_id, row)

    # Reading (the store holds the lock) ---------------------------------

    def get_alert(self, alert_id: str) -> AlertRecord | None:
        """Return the alert with this id, or ``None``."""
        record_id = self._alert_ids.get(alert_id)
        if record_id is None:
            return None
        return alert_from_row(self._alerts.get(record_id).data)

    def alerts_for(self, run_id: str) -> list[AlertRecord]:
        """Return a run's alerts, oldest first."""
        records = _by_index(self._alerts, "alerts_run_id", run_id)
        return _oldest_first([alert_from_row(r.data) for r in records])

    def query_alerts(
        self,
        *,
        kind: AlertKind | None,
        job: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[AlertRecord]:
        """Return alerts matching every given filter, newest first."""
        records = self._raised_between(EARLIEST if since is None else since, LATEST)
        alerts = [alert_from_row(r.data) for r in records]
        matching = [
            a
            for a in alerts
            if (kind is None or a.kind == kind)
            and (job is None or a.job == job)
            and (since is None or a.raised_at >= since)
        ]
        return list(reversed(_oldest_first(matching)))[:limit]

    def alert_ids_since(self, when: datetime) -> set[str]:
        """Return the ids of alerts raised at ``when`` or later."""
        return {cast(str, r["alert_id"]) for r in self._raised_between(when, LATEST)}

    def runless_alert_ids_before(self, when: datetime, limit: int) -> list[str]:
        """Return up to ``limit`` ids of run-less alerts raised before ``when``, oldest first."""
        alerts = [alert_from_row(r.data) for r in self._raised_between(EARLIEST, when)]
        old = [a for a in alerts if a.run_id is None and a.raised_at < when]
        return [a.alert_id for a in _oldest_first(old)[:limit]]

    def deliveries_for(self, alert_ids: Collection[str]) -> dict[str, list[DeliveryRecord]]:
        """Return the deliveries of each alert in ``alert_ids`` that has any."""
        found: dict[str, list[DeliveryRecord]] = {}
        for alert_id in alert_ids:
            records = _by_index(self._deliveries, "deliveries_alert_id", alert_id)
            deliveries = sorted(
                (delivery_from_row(r.data) for r in records), key=lambda d: d.delivery_id
            )
            if deliveries:
                found[alert_id] = deliveries
        return found

    def deliveries_with(self, status: DeliveryStatus, limit: int) -> list[DeliveryRecord]:
        """Return up to ``limit`` deliveries in ``status``, longest-waiting first."""
        records = _by_index(self._deliveries, "deliveries_status", status.value)
        deliveries = [delivery_from_row(r.data) for r in records]
        deliveries.sort(key=lambda d: (d.updated_at, d.delivery_id))
        return deliveries[:limit]

    def run_files(self, run_ids: Collection[str]) -> dict[str, tuple[str, ...]]:
        """Return the matched files of each run in ``run_ids`` that has some."""
        found: dict[str, tuple[str, ...]] = {}
        for run_id in run_ids:
            record_id = self._file_ids.get(run_id)
            if record_id is not None:
                found[run_id] = tuple(json.loads(cast(str, self._files.get(record_id)["files"])))
        return found

    def _raised_between(self, low: datetime, high: datetime) -> list[Record]:
        """Return alert records raised from ``low`` to ``high``, inclusive."""
        index = self._alerts.indexes["alerts_raised_at"]
        return [self._alerts.get(record_id) for record_id in index.find_range(low, high)]


def _by_index(table: Table, index_name: str, key: Value) -> list[Record]:
    """Return the table's records whose indexed column equals ``key``."""
    return table.select_by_index(table.indexes[index_name], key)


def _oldest_first(alerts: list[AlertRecord]) -> list[AlertRecord]:
    """Return ``alerts`` sorted by raised-at, then id."""
    return sorted(alerts, key=lambda a: (a.raised_at, a.alert_id))
