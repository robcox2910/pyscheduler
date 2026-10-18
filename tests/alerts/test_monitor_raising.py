"""Tests for raising an alert: once per id, even after the monitor forgot it, and safely."""

from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from alert_helpers import RecordingNotifier
from engine_helpers import START
from monitor_helpers import monitored, raised, run_monitor, step

from pyscheduler.alerts import raising
from pyscheduler.alerts.model import AlertRecord, DeliveryStatus
from pyscheduler.alerts.monitor import PRUNE_EVERY, AlertMonitor
from pyscheduler.config import Config
from pyscheduler.testing import FakeResult

ACTOR = "tester"
ONE_MINUTE = timedelta(minutes=1)
FAILED_EXIT = 1
LOOKS = 200
NAP_SECONDS = 0.01

FAILING = """\
jobs:
  f: {command: "false", schedule: manual}
"""


async def sent_once(h_monitor: AlertMonitor, alert_id: str) -> None:
    """Wait until ``alert_id``'s delivery is ``sent``."""
    store = h_monitor.engine.store
    for _ in range(LOOKS):
        found = await pynet.loop.run_in_thread(store.deliveries_for, [alert_id])
        if [d.status for d in found.get(alert_id, [])] == [DeliveryStatus.SENT]:
            return
        await pynet.loop.sleep(NAP_SECONDS)
    msg = f"{alert_id} was never sent"
    raise AssertionError(msg)


class TestRaisingOnce:
    """An alert already in the store is never written again."""

    def test_forgotten_alert_raised_again_keeps_its_sent_delivery(self, tmp_path: Path) -> None:
        """Leave a ``sent`` delivery alone when the same alert id is raised after a prune."""
        h = monitored(tmp_path, FAILING)
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))
        notifier = RecordingNotifier()

        async def body(monitor: AlertMonitor) -> None:
            """Fail ``f``, wait for the send, forget the id, raise the same alert again."""
            await h.engine.trigger("f", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            [alert] = await raised(h, monitor)
            await sent_once(monitor, alert.alert_id)
            monitor._seen.clear()  # as after the hourly prune
            monitor._raise(alert)
            await monitor.settle()
            await monitor.dispatcher.settle()
            found = await pynet.loop.run_in_thread(h.store.deliveries_for, [alert.alert_id])
            [delivery] = found[alert.alert_id]
            assert delivery.status is DeliveryStatus.SENT
            assert len(notifier.received) == 1

        run_monitor(h, body, notifier=notifier)


class TestRaisingSafely:
    """Trouble inside the monitor is logged, and its rounds carry on."""

    def test_routing_error_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log an alert whose routing fails; the engine carries on."""
        h = monitored(tmp_path, FAILING)
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))

        def broken(config: Config, alert: AlertRecord) -> list[str]:
            """Fail to route."""
            del config, alert
            msg = "no route"
            raise RuntimeError(msg)

        monkeypatch.setattr(raising, "destinations", broken)

        async def body(monitor: AlertMonitor) -> None:
            """Fail ``f`` and find the logged error and no stored alert."""
            await h.engine.trigger("f", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            assert await raised(h, monitor) == []
            assert h.engine.error is None

        run_monitor(h, body)
        assert "could not route alert" in caplog.text

    def test_failed_prune_plans_the_next(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Plan the next hourly trim even when one trim fails."""
        h = monitored(tmp_path, FAILING)

        class Brittle(set[str]):
            """A set of ids whose first walk-through fails."""

            walks = 0

            def __iter__(self):
                """Fail the first time, then walk as usual."""
                Brittle.walks += 1
                if Brittle.walks == 1:
                    msg = "brittle"
                    raise RuntimeError(msg)
                return super().__iter__()

        async def body(monitor: AlertMonitor) -> None:
            """Break the first trim, then check a second one ran."""
            monitor._seen = Brittle({"gone.2026-10-19.1:failed:1"})
            await step(h, monitor, START + 2 * PRUNE_EVERY + ONE_MINUTE)
            assert type(monitor._seen) is set
            assert monitor._seen == set()

        run_monitor(h, body)
        assert "alert monitor timer failed" in caplog.text
