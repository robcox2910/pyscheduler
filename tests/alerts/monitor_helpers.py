"""Run an engine with a dispatcher and an alert monitor, and read the alerts it raised."""

import textwrap
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pynet.loop
from alert_helpers import SCALE, RecordingNotifier, factory_for, make_harness
from engine_helpers import Harness

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import AlertRecord
from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.config.model import AlertKind

ROUTED = """\
notifiers:
  rec: {type: log, path: logs/alerts.log}
routes:
  - {match: "alert.#", notify: [rec]}
"""
"""One notifier, ``rec``, that every alert is routed to."""

type MonitorBody = Callable[[AlertMonitor], Coroutine[Any, Any, None]]


def monitored(tmp_path: Path, jobs: str, env: Mapping[str, str] | None = None) -> Harness:
    """Build a harness whose config routes every alert to ``rec``, then ``jobs``."""
    return make_harness(tmp_path, ROUTED + textwrap.dedent(jobs), env)


def run_monitor(
    h: Harness,
    body: MonitorBody,
    *,
    notifier: RecordingNotifier | None = None,
    at: datetime | None = None,
) -> None:
    """Run ``body`` with the engine, the dispatcher and the monitor started.

    The monitor is built before the engine starts, so it hears recovery.
    With ``at`` the fake clock first moves there (a restart after downtime).
    """
    factory = factory_for({"rec": notifier or RecordingNotifier()})

    async def main() -> None:
        """Start everything, run the body, stop in reverse order."""
        if at is not None:
            await h.clock.advance_to(at)
        dispatcher = Dispatcher(h.engine, factory=factory, time_scale=SCALE)
        monitor = AlertMonitor(h.engine, dispatcher)
        await h.engine.start()
        try:
            await dispatcher.start()
            await monitor.start()
            try:
                await h.engine.settle()
                await monitor.settle()
                await body(monitor)
            finally:
                await monitor.stop()
                await dispatcher.stop()
        finally:
            await h.engine.stop()

    pynet.loop.run(main())


async def raised(
    h: Harness, monitor: AlertMonitor, kind: AlertKind | None = None
) -> list[AlertRecord]:
    """Settle the engine and the monitor, then return the stored alerts, oldest first."""
    await h.engine.settle()
    await monitor.settle()
    found = await pynet.loop.run_in_thread(h.engine.store.query_alerts, kind=kind)
    return sorted(found, key=lambda a: (a.raised_at, a.alert_id))


async def step(h: Harness, monitor: AlertMonitor, when: datetime) -> None:
    """Move the clock to ``when`` and let the monitor finish what that raised."""
    await h.advance_to(when)
    await monitor.settle()
