"""Tests for the alert tables: alerts, deliveries and matched files."""

from datetime import timedelta
from pathlib import Path

import pytest
from alert_helpers import RAISED, make_alert
from store_helpers import T0, make_run

from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import AlertKind, Criticality
from pyscheduler.engine.model import RunState
from pyscheduler.store import Batch, Store

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
TWO = 2
LIMIT_ONE = 1
CUTOFF = RAISED + DAY


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Open a fresh store without fsync."""
    return Store.open(tmp_path / "state", sync=False)


def delivery(
    alert_id: str,
    notifier: str,
    status: DeliveryStatus = DeliveryStatus.PENDING,
    *,
    attempts: int = 0,
) -> DeliveryRecord:
    """Return a delivery of ``alert_id`` to ``notifier``."""
    return DeliveryRecord(alert_id, notifier, status, attempts, "", RAISED)


class TestRoundTrip:
    """Alerts, deliveries and files survive a reopen."""

    def test_round_trip(self, store: Store, tmp_path: Path) -> None:
        """Save one alert, two deliveries and run files; read them back after reopening."""
        alert = make_alert()
        sent = delivery(alert.alert_id, "ops-slack")
        wait = delivery(alert.alert_id, "team-log")
        files = {"load.20261017.1": ("/in/a.csv", "/in/b.csv")}
        store.apply(Batch(alerts=[alert], deliveries=[sent, wait], run_files=files))
        store.close()
        store = Store.open(tmp_path / "state", sync=False)
        assert store.get_alert(alert.alert_id) == alert
        assert store.deliveries_for([alert.alert_id]) == {alert.alert_id: [sent, wait]}
        assert store.run_files(["load.20261017.1", "nope"]) == files
        assert store.deliveries_with(DeliveryStatus.PENDING) == [sent, wait]
        assert store.get_alert("missing") is None
        assert sent.delivery_id == f"{alert.alert_id}|ops-slack"

    def test_upsert_is_idempotent(self, store: Store) -> None:
        """Apply the same alert twice and a delivery twice: one row each, latest wins."""
        alert = make_alert()
        store.apply(Batch(alerts=[alert], deliveries=[delivery(alert.alert_id, "team-log")]))
        done = delivery(alert.alert_id, "team-log", DeliveryStatus.SENT, attempts=1)
        store.apply(Batch(alerts=[alert], deliveries=[done], run_files={"r": ("x",)}))
        store.apply(Batch(run_files={"r": ("y",)}))
        assert store.alerts_for(alert.run_id or "") == [alert]
        assert store.deliveries_for([alert.alert_id]) == {alert.alert_id: [done]}
        assert store.deliveries_with(DeliveryStatus.PENDING) == []
        assert store.run_files(["r"]) == {"r": ("y",)}


class TestQueries:
    """Alerts are found by kind, job and time."""

    def test_query_alerts(self, store: Store) -> None:
        """Filter by kind, job and since; newest first; honour the limit."""
        old = make_alert(raised_at=RAISED)
        mid = make_alert(
            AlertKind.TOO_LONG, criticality=Criticality.NORMAL, raised_at=RAISED + HOUR
        )
        other = make_alert(job="other", run_id="other.20261017.1", raised_at=RAISED + TWO * HOUR)
        store.apply(Batch(alerts=[old, mid, other]))
        assert store.query_alerts() == [other, mid, old]
        assert store.query_alerts(kind=AlertKind.TOO_LONG) == [mid]
        assert store.query_alerts(job="load") == [mid, old]
        assert store.query_alerts(since=RAISED + HOUR) == [other, mid]
        assert store.query_alerts(limit=LIMIT_ONE) == [other]
        assert store.alert_ids_since(RAISED + HOUR) == {other.alert_id, mid.alert_id}

    def test_runless_alert_ids_before(self, store: Store) -> None:
        """List run-less alerts older than the cutoff, oldest first, up to the limit."""
        old = make_alert(AlertKind.RELOAD_FAILED, run_id=None, job=None, raised_at=RAISED)
        older = make_alert(AlertKind.RELOAD_FAILED, run_id=None, job=None, raised_at=RAISED - HOUR)
        tied = make_alert(raised_at=RAISED - DAY)
        store.apply(Batch(alerts=[old, older, tied]))
        assert store.runless_alert_ids_before(CUTOFF, 10) == [older.alert_id, old.alert_id]
        assert store.runless_alert_ids_before(CUTOFF, LIMIT_ONE) == [older.alert_id]
        assert store.runless_alert_ids_before(RAISED, 10) == [older.alert_id]

    def test_dead_deliveries_oldest_first(self, store: Store) -> None:
        """List deliveries of a status oldest update first."""
        alert = make_alert()
        late = DeliveryRecord(alert.alert_id, "a", DeliveryStatus.DEAD, 5, "boom", RAISED + HOUR)
        early = DeliveryRecord(alert.alert_id, "b", DeliveryStatus.DEAD, 5, "boom", RAISED)
        store.apply(Batch(alerts=[alert], deliveries=[late, early]))
        assert store.deliveries_with(DeliveryStatus.DEAD) == [early, late]
        assert store.deliveries_with(DeliveryStatus.DEAD, limit=LIMIT_ONE) == [early]


class TestBatchMerge:
    """Merging keeps the last record per key."""

    def test_merge_keeps_last(self) -> None:
        """Keep the later alert, delivery and file list; drop those of a deleted run."""
        alert = make_alert()
        first = delivery(alert.alert_id, "n")
        last = delivery(alert.alert_id, "n", DeliveryStatus.SENT)
        batch = Batch(alerts=[alert], deliveries=[first], run_files={"r": ("a",)})
        batch.merge(Batch(alerts=[alert], deliveries=[last], run_files={"r": ("b",)}))
        assert batch.alerts == [alert]
        assert batch.deliveries == [last]
        assert batch.run_files == {"r": ("b",)}
        assert not batch.is_empty
        batch.merge(Batch(deleted_run_ids=[alert.run_id or ""], deleted_alert_ids=["x"]))
        assert batch.alerts == []
        assert batch.deliveries == []
        assert batch.run_files == {"r": ("b",)}
        assert batch.deleted_alert_ids == ["x"]


class TestDeletes:
    """Deleting a run or an alert takes its dependents with it."""

    def test_delete_run_takes_alerts(self, store: Store) -> None:
        """Remove a run's alerts, their deliveries and its files; keep others."""
        run = make_run(state=RunState.SUCCEEDED)
        other = make_run("other", state=RunState.SUCCEEDED)
        alert = make_alert(run_id=run.run_id)
        kept = make_alert(job="other", run_id=other.run_id)
        deliveries = [delivery(alert.alert_id, "n"), delivery(kept.alert_id, "n")]
        files = {run.run_id: ("a",), other.run_id: ("b",)}
        store.apply(
            Batch(runs=[run, other], alerts=[alert, kept], deliveries=deliveries, run_files=files)
        )
        store.apply(Batch(deleted_run_ids=[run.run_id]))
        assert store.alerts_for(run.run_id) == []
        assert store.deliveries_for([alert.alert_id, kept.alert_id]) == {
            kept.alert_id: [deliveries[1]]
        }
        assert store.run_files([run.run_id, other.run_id]) == {other.run_id: ("b",)}
        assert store.alerts_for(other.run_id) == [kept]

    def test_delete_alert_ids(self, store: Store) -> None:
        """Remove run-less alerts and their deliveries by id."""
        alert = make_alert(AlertKind.RELOAD_FAILED, run_id=None, job=None, raised_at=T0)
        store.apply(Batch(alerts=[alert], deliveries=[delivery(alert.alert_id, "n")]))
        store.apply(Batch(deleted_alert_ids=[alert.alert_id]))
        assert store.get_alert(alert.alert_id) is None
        assert store.deliveries_for([alert.alert_id]) == {}
        store.apply(Batch(alerts=[alert]))  # the address book forgot it, so it inserts afresh
        assert store.get_alert(alert.alert_id) == alert
