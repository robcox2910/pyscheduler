"""Tests for the alert monitor across reloads, restarts of old runs, and its own lifecycle."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from alert_helpers import SCALE, RecordingNotifier, factory_for
from engine_helpers import START, TODAY, Harness
from monitor_helpers import MonitorBody, monitored, raised, reconfigured, run_monitor, step
from recovery_helpers import make_run, seed

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus, alert_id_for
from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.config.model import AlertKind, Criticality
from pyscheduler.engine.model import Attempt, RunState, Trigger
from pyscheduler.store import Batch
from pyscheduler.testing import FakeResult

ACTOR = "tester"
ONE_MINUTE = timedelta(minutes=1)
ONE_SECOND = timedelta(seconds=1)
ONE_HOUR = timedelta(hours=1)
ONE = 1
SECOND_ATTEMPT = 2
FAILED_EXIT = 1
OLD_DAYS = 30

SLOW = """\
jobs:
  slow: {command: x, schedule: manual, max_runtime: 5m}
  other: {command: x, schedule: manual}
"""

WAITING = """\
jobs:
  up: {command: x, schedule: manual}
  a: {command: x, schedule: manual, must_start_by: 30m, depends_on: [up]}
"""


class TestReloads:
    """Timers follow the config in force when they ring, not the one they were set under."""

    @pytest.mark.parametrize(
        "after",
        [
            "jobs:\n  slow: {command: x, schedule: manual}\n  other: {command: x, schedule: manual}\n",
            "jobs:\n  other: {command: x, schedule: manual}\n",
        ],
        ids=["max_runtime-removed", "job-removed"],
    )
    def test_reload_calls_off_too_long(self, tmp_path: Path, after: str) -> None:
        """Raise no ``too_long`` once a reload drops ``max_runtime`` or the whole job."""
        h = monitored(tmp_path, SLOW)
        h.launcher.plan("slow", FakeResult(duration=7 * ONE_MINUTE))

        async def body(monitor: AlertMonitor) -> None:
            """Start the job, reload at 2 m, and run past 5 m."""
            await h.engine.trigger("slow", actor=ACTOR)
            await step(h, monitor, START + 2 * ONE_MINUTE)
            await h.engine.reload(reconfigured(tmp_path, after), actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            assert await raised(h, monitor, AlertKind.TOO_LONG) == []

        run_monitor(h, body)

    def test_reload_moves_max_runtime_earlier(self, tmp_path: Path) -> None:
        """Raise ``too_long`` at the new, earlier limit after a reload."""
        h = monitored(tmp_path, SLOW)
        h.launcher.plan("slow", FakeResult(duration=7 * ONE_MINUTE))

        async def body(monitor: AlertMonitor) -> None:
            """Start the job, shorten the limit to 3 m, and check at 3 m."""
            run = await h.engine.trigger("slow", actor=ACTOR)
            await step(h, monitor, START + ONE_MINUTE)
            await h.engine.reload(reconfigured(tmp_path, SLOW.replace("5m", "3m")), actor=ACTOR)
            await step(h, monitor, START + 3 * ONE_MINUTE)
            alerts = await raised(h, monitor, AlertKind.TOO_LONG)
            assert [a.alert_id for a in alerts] == [f"{run.run_id}:too_long:1"]
            assert alerts[0].detail["max_runtime_seconds"] == (3 * ONE_MINUTE).total_seconds()

        run_monitor(h, body)

    @pytest.mark.parametrize("minutes", [45, 20], ids=["later", "earlier"])
    def test_reload_moves_must_start_by(self, tmp_path: Path, minutes: int) -> None:
        """Raise ``never_started`` at the deadline the reload set, not the old one."""
        h = monitored(tmp_path, WAITING)

        async def body(monitor: AlertMonitor) -> None:
            """Let ``a`` wait, move its deadline at 10 m, and check around both times."""
            a = await h.engine.trigger("a", actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            moved = WAITING.replace("30m", f"{minutes}m")
            await h.engine.reload(reconfigured(tmp_path, moved), actor=ACTOR)
            due = START + minutes * ONE_MINUTE
            await step(h, monitor, min(due, START + 30 * ONE_MINUTE) - ONE_SECOND)
            await step(h, monitor, due - ONE_SECOND)
            assert await raised(h, monitor) == []
            await step(h, monitor, due)
            alerts = await raised(h, monitor)
            assert [x.alert_id for x in alerts] == [f"{a.run_id}:never_started:0"]
            assert alerts[0].raised_at == due

        run_monitor(h, body)


class TestOldOpenRuns:
    """An open run older than the memory window is not alerted twice."""

    def test_old_open_run_alert_not_repeated(self, tmp_path: Path) -> None:
        """Keep the old ``never_started`` alert, and its sent delivery, after a restart."""
        h = monitored(tmp_path, WAITING)
        day = TODAY - timedelta(days=OLD_DAYS)
        due = START - timedelta(days=OLD_DAYS)
        old = make_run("a", RunState.WAITING, run_date=day, scheduled_at=due)
        old = replace(old, trigger=Trigger.MANUAL)
        alert_id = alert_id_for(old.run_id, AlertKind.NEVER_STARTED, 0)
        raised_at = due + 30 * ONE_MINUTE
        alert = AlertRecord(
            alert_id=alert_id,
            kind=AlertKind.NEVER_STARTED,
            run_id=old.run_id,
            job="a",
            run_date=day,
            attempt=0,
            criticality=Criticality.NORMAL,
            raised_at=raised_at,
            message="late",
            detail={},
        )
        sent = DeliveryRecord(alert_id, "rec", DeliveryStatus.SENT, ONE, "", raised_at)
        seed(h, Batch(runs=[old], alerts=[alert], deliveries=[sent]))
        rec = RecordingNotifier()

        async def body(monitor: AlertMonitor) -> None:
            """Find the old alert untouched and nothing sent."""
            assert h.engine.book.get(old.run_id) is not None
            alerts = await raised(h, monitor)
            assert [(a.alert_id, a.raised_at) for a in alerts] == [(alert_id, raised_at)]
            found = await pynet.loop.run_in_thread(h.store.deliveries_for, [alert_id])
            assert [(d.status, d.attempts) for d in found[alert_id]] == [(DeliveryStatus.SENT, ONE)]

        run_monitor(h, body, notifier=rec)
        assert rec.received == []


def run_late_start(h: Harness, before: MonitorBody, after: MonitorBody) -> None:
    """Build the monitor before the engine starts, run ``before``, then start the monitor."""

    async def main() -> None:
        """Start the engine and dispatcher, run ``before``, start the monitor, run ``after``."""
        dispatcher = Dispatcher(
            h.engine, factory=factory_for({"rec": RecordingNotifier()}), time_scale=SCALE
        )
        monitor = AlertMonitor(h.engine, dispatcher)
        await h.engine.start()
        try:
            await dispatcher.start()
            try:
                await before(monitor)
                await monitor.start()
                await h.engine.settle()
                await monitor.settle()
                await after(monitor)
            finally:
                await monitor.stop()
                await dispatcher.stop()
        finally:
            await h.engine.stop()

    pynet.loop.run(main())


class TestLifecycle:
    """Starting late and stopping early leave nothing behind."""

    def test_stale_attempt_is_not_timed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Arm no ``too_long`` timer for an attempt that ended while ``start`` read the store."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              slow:
                command: x
                schedule: manual
                max_runtime: 5m
                retries: {count: 1, delay: 1m}
            """,
        )
        h.launcher.plan("slow", FakeResult(exit_code=FAILED_EXIT), FakeResult(duration=ONE_HOUR))
        first: list[Attempt] = []
        real = h.store.latest_attempts

        def stale(run_ids: list[str]) -> dict[str, Attempt]:
            """Return the first attempt as if it were still running (it ended)."""
            found = real(run_ids)
            return {run_id: replace(first[0], ended_at=None) for run_id in found}

        async def before(_: AlertMonitor) -> None:
            """Run the job into its second attempt before the monitor starts."""
            await h.engine.trigger("slow", actor=ACTOR)
            await h.advance(3 * ONE_MINUTE)
            assert h.run("slow").attempt == SECOND_ATTEMPT
            attempts = await pynet.loop.run_in_thread(h.store.attempts_for, h.run("slow").run_id)
            first.append(attempts[0])
            monkeypatch.setattr(h.store, "latest_attempts", stale)

        async def after(_: AlertMonitor) -> None:
            """Find only the second attempt's timer (and the prune) set."""
            latest = await pynet.loop.run_in_thread(real, [h.run("slow").run_id])
            second = latest[h.run("slow").run_id]
            assert h.clock.next_due() == second.started_at + 5 * ONE_MINUTE

        run_late_start(h, before, after)

    def test_stop_before_start(self, tmp_path: Path) -> None:
        """Drop what was heard early when stopped before ``start``; raise nothing after."""
        h = monitored(tmp_path, "jobs:\n  f: {command: x, schedule: manual}\n")
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))

        async def main() -> None:
            """Fail a job while the monitor is unstarted, stop it, then start it."""
            dispatcher = Dispatcher(h.engine, time_scale=SCALE)
            monitor = AlertMonitor(h.engine, dispatcher)
            await h.engine.start()
            try:
                await dispatcher.start()
                await h.engine.trigger("f", actor=ACTOR)
                await h.advance(5 * ONE_MINUTE)
                assert h.run("f").state is RunState.FAILED
                await monitor.stop()
                await monitor.start()
                monitor.reload_failed([])
                await monitor.stop()
                await dispatcher.stop()
            finally:
                await h.engine.stop()

        pynet.loop.run(main())
        with h.reopened() as store:
            assert store.query_alerts() == []
