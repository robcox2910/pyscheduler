"""Tests for alerts while the scheduler shuts down: nothing that ends then goes unalerted."""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pynet.loop
from alert_helpers import SCALE, RecordingNotifier, factory_for
from engine_helpers import START
from monitor_helpers import monitored, run_monitor, step
from pynet.messaging import JsonValue
from recovery_helpers import restarted

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import DeliveryStatus
from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.config.model import AlertKind
from pyscheduler.engine.model import RunState
from pyscheduler.service import _Service
from pyscheduler.testing import FakeResult

ACTOR = "tester"
ONE_MINUTE = timedelta(minutes=1)
FAILED_EXIT = 1
STOP_STARTED_SECONDS = 0.1
RESEND_LOOKS = 200
RESEND_NAP_SECONDS = 0.01

JOBS = """\
jobs:
  first: {command: "false", schedule: manual}
  second: {command: "false", schedule: manual}
"""


class GatedNotifier(RecordingNotifier):
    """Hold every send until ``gate`` is set, so the dispatcher's stop has to wait."""

    def __init__(self) -> None:
        """Start closed."""
        super().__init__()
        self.gate = pynet.loop.Event()

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Wait for the gate, then record."""
        await self.gate.wait()
        await super().send(alert)


class TestShutdown:
    """A run that ends while ``serve`` shuts down still gets its alert."""

    def test_run_failing_during_shutdown_is_alerted_after_restart(self, tmp_path: Path) -> None:
        """``second`` fails while the dispatcher waits for a send: its alert is stored pending.

        The next start sends it.
        """
        h = monitored(tmp_path, JOBS)
        h.launcher.plan("first", FakeResult(duration=ONE_MINUTE, exit_code=FAILED_EXIT))
        h.launcher.plan("second", FakeResult(duration=2 * ONE_MINUTE, exit_code=FAILED_EXIT))
        notifier = GatedNotifier()
        second_id: list[str] = []

        async def main() -> None:
            """Fail ``first``, start shutting down, fail ``second`` meanwhile, finish."""
            dispatcher = Dispatcher(
                h.engine, factory=factory_for({"rec": notifier}), time_scale=SCALE
            )
            monitor = AlertMonitor(h.engine, dispatcher)
            await h.engine.start()
            await dispatcher.start()
            await monitor.start()
            await h.engine.trigger("first", actor=ACTOR)
            second_id.append((await h.engine.trigger("second", actor=ACTOR)).run_id)
            await step(h, monitor, START + ONE_MINUTE)  # first's alert is being sent (held)
            service = _Service()
            service.engine, service.dispatcher, service.monitor = h.engine, dispatcher, monitor
            stopping = pynet.loop.spawn(service.shut_down(), name="shut-down")
            await pynet.loop.sleep(STOP_STARTED_SECONDS)  # the dispatcher now waits for the send
            await h.advance(ONE_MINUTE)
            assert h.run("second").state is RunState.FAILED
            notifier.gate.set()
            await stopping

        pynet.loop.run(main())
        with h.reopened() as store:
            [alert] = [
                a for a in store.query_alerts(kind=AlertKind.FAILED) if a.run_id in second_id
            ]
            [delivery] = store.deliveries_for([alert.alert_id])[alert.alert_id]
            assert delivery.status is DeliveryStatus.PENDING
        again = restarted(h)
        resent = RecordingNotifier()

        async def after(monitor: AlertMonitor) -> None:
            """Wait until the restarted dispatcher has sent ``second``'s alert."""
            del monitor
            for _ in range(RESEND_LOOKS):
                if resent.received:
                    return
                await pynet.loop.sleep(RESEND_NAP_SECONDS)

        run_monitor(again, after, notifier=resent)
        assert [a["run_id"] for a in resent.received] == second_id
