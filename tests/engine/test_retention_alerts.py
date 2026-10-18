"""Test that pruning takes alerts, deliveries and matched files with it."""

from datetime import timedelta

import pynet.loop
from alert_helpers import make_alert
from engine_helpers import START, TODAY, MakeHarness
from recovery_helpers import make_run, seed

from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import AlertKind
from pyscheduler.engine.model import RunState
from pyscheduler.store import Batch

OLD = TODAY - timedelta(days=100)
PLAIN = """
jobs:
  m:
    command: run-m
    schedule: manual
"""


def pending(alert_id: str) -> DeliveryRecord:
    """Return a pending delivery of ``alert_id``."""
    return DeliveryRecord(alert_id, "n", DeliveryStatus.PENDING, 0, "", START)


class TestPruneAlerts:
    """Alerts live as long as their run, or by their own age if they have none."""

    def test_prune_deletes_alerts_with_their_run(self, harness: MakeHarness) -> None:
        """Remove a pruned run's alerts, deliveries and files at start."""
        h = harness(PLAIN)
        old = make_run("m", RunState.FAILED, run_date=OLD)
        alert = make_alert(run_id=old.run_id, job="m", raised_at=START)
        batch = Batch(
            runs=[old],
            alerts=[alert],
            deliveries=[pending(alert.alert_id)],
            run_files={old.run_id: ("/in/a",)},
        )
        seed(h, batch)

        async def body() -> None:
            """Check the store has forgotten all three."""
            assert await pynet.loop.run_in_thread(h.store.get_alert, alert.alert_id) is None
            ids = [alert.alert_id]
            assert await pynet.loop.run_in_thread(h.store.deliveries_for, ids) == {}
            assert await pynet.loop.run_in_thread(h.store.run_files, [old.run_id]) == {}

        h.run_engine(body)

    def test_prune_deletes_old_runless_alerts(self, harness: MakeHarness) -> None:
        """Remove run-less alerts older than retention; keep newer ones."""
        h = harness(PLAIN)
        kind = AlertKind.RELOAD_FAILED
        stale = make_alert(kind, run_id=None, job=None, raised_at=START - timedelta(days=100))
        fresh = make_alert(kind, run_id=None, job=None, raised_at=START - timedelta(days=1))
        seed(h, Batch(alerts=[stale, fresh], deliveries=[pending(stale.alert_id)]))

        async def body() -> None:
            """Check only the stale alert, and its delivery, went."""
            assert await pynet.loop.run_in_thread(h.store.get_alert, stale.alert_id) is None
            assert await pynet.loop.run_in_thread(h.store.get_alert, fresh.alert_id) == fresh
            ids = [stale.alert_id]
            assert await pynet.loop.run_in_thread(h.store.deliveries_for, ids) == {}

        h.run_engine(body)
