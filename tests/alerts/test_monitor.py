"""Tests for the alert monitor: each kind, deadlines, dedupe, recovery and store trouble."""

from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from alert_helpers import RecordingNotifier
from engine_helpers import START, TODAY
from monitor_helpers import monitored, raised, run_monitor, step
from recovery_helpers import dead_pid, make_attempt, make_run, restarted, seed

from pyscheduler.alerts import deadlines
from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.alerts.routing import alert_topic
from pyscheduler.config.model import AlertKind, Criticality, Problem
from pyscheduler.engine.model import RunState
from pyscheduler.store import Batch
from pyscheduler.testing import FakeResult

ACTOR = "tester"
ONE_MINUTE = timedelta(minutes=1)
TWO_HOURS = timedelta(hours=2)
ONE = 1
FAILED_EXIT = 1
SLOW_NOTIFIER_SECONDS = 2.0
WAIT_FAILED_SECONDS = 5.0
HIDDEN = "s3cr3t-value"

FAILING = """\
jobs:
  f:
    command: "false"
    schedule: manual
    retries: {count: 1, delay: 1m}
"""


class TestRunKinds:
    """Failed, retrying and blocked alerts follow the run's state."""

    def test_failed_alert_once(self, tmp_path: Path) -> None:
        """Raise one ``failed`` (no ``retrying``); a rerun that fails again raises another.

        The rerun gets its own retry, so its final attempt is the fourth.
        """
        h = monitored(tmp_path, FAILING)
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))

        async def body(monitor: AlertMonitor) -> None:
            """Fail the job, check the alerts, rerun it, check again."""
            run = await h.engine.trigger("f", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            assert h.run("f").state is RunState.FAILED
            first = await raised(h, monitor)
            assert [a.alert_id for a in first] == [f"{run.run_id}:failed:2"]
            assert first[0].message == f"{run.run_id} failed: exit 1"
            await h.engine.rerun(run.run_id, actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            ids = [a.alert_id for a in await raised(h, monitor)]
            assert ids == [f"{run.run_id}:failed:2", f"{run.run_id}:failed:4"]

        run_monitor(h, body)

    def test_retrying_when_enabled(self, tmp_path: Path) -> None:
        """Raise one ``retrying`` alert when the job turns the kind on."""
        h = monitored(
            tmp_path, FAILING.replace("    retries:", "    alerts: {on: [retrying]}\n    retries:")
        )
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))

        async def body(monitor: AlertMonitor) -> None:
            """Fail the job twice and count the retrying alerts."""
            run = await h.engine.trigger("f", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            retrying = await raised(h, monitor, AlertKind.RETRYING)
            assert [a.alert_id for a in retrying] == [f"{run.run_id}:retrying:1"]
            assert run.run_id in retrying[0].message

        run_monitor(h, body)

    def test_off_kind_raises_nothing(self, tmp_path: Path) -> None:
        """Store no alert at all for a kind the job turns off."""
        h = monitored(
            tmp_path, FAILING.replace("    retries:", "    alerts: {off: [failed]}\n    retries:")
        )
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))

        async def body(monitor: AlertMonitor) -> None:
            """Fail the job and find no alert rows."""
            await h.engine.trigger("f", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            assert h.run("f").state is RunState.FAILED
            assert await raised(h, monitor) == []

        run_monitor(h, body)

    def test_blocked_alert(self, tmp_path: Path) -> None:
        """Raise one ``blocked`` alert when the upstream fails."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              up: {command: "false", schedule: manual}
              down: {command: "true", depends_on: [up]}
            """,
        )
        h.launcher.plan("up", FakeResult(exit_code=FAILED_EXIT))

        async def body(monitor: AlertMonitor) -> None:
            """Fail the upstream and check the downstream's alert."""
            await h.engine.trigger("up", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            down = h.run("down")
            assert down.state is RunState.BLOCKED
            blocked = await raised(h, monitor, AlertKind.BLOCKED)
            assert [a.alert_id for a in blocked] == [f"{down.run_id}:blocked:0"]
            assert down.run_id in blocked[0].message

        run_monitor(h, body)


class TestAttemptKinds:
    """Too long, too short and lost alerts follow the attempts."""

    def test_too_long_fires_while_running(self, tmp_path: Path) -> None:
        """Raise ``too_long`` after 5 m while the 7 m job still runs; a 3 m job raises none."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              slow: {command: x, schedule: manual, max_runtime: 5m, timeout: 10m}
              quick: {command: x, schedule: manual, max_runtime: 5m, timeout: 10m}
            """,
        )
        h.launcher.plan("slow", FakeResult(duration=7 * ONE_MINUTE))
        h.launcher.plan("quick", FakeResult(duration=3 * ONE_MINUTE))

        async def body(monitor: AlertMonitor) -> None:
            """Run both jobs past five minutes and check the alerts."""
            slow = await h.engine.trigger("slow", actor=ACTOR)
            await h.engine.trigger("quick", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE - timedelta(seconds=1))
            assert await raised(h, monitor) == []
            await step(h, monitor, START + 5 * ONE_MINUTE)
            assert h.run("slow").state is RunState.RUNNING
            alerts = await raised(h, monitor)
            assert [a.alert_id for a in alerts] == [f"{slow.run_id}:too_long:1"]
            assert alerts[0].detail["max_runtime_seconds"] == (5 * ONE_MINUTE).total_seconds()
            assert slow.run_id in alerts[0].message
            await step(h, monitor, START + 10 * ONE_MINUTE)
            assert len(await raised(h, monitor)) == ONE

        run_monitor(h, body)

    def test_too_short(self, tmp_path: Path) -> None:
        """Raise ``too_short`` for a 1 m success under ``min_runtime: 2m``; not for a failure."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              ok: {command: x, schedule: manual, min_runtime: 2m}
              bad: {command: x, schedule: manual, min_runtime: 2m}
            """,
        )
        h.launcher.plan("bad", FakeResult(exit_code=FAILED_EXIT))

        async def body(monitor: AlertMonitor) -> None:
            """Run both jobs for a minute and check the alerts."""
            ok = await h.engine.trigger("ok", actor=ACTOR)
            await h.engine.trigger("bad", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            short = await raised(h, monitor, AlertKind.TOO_SHORT)
            assert [a.alert_id for a in short] == [f"{ok.run_id}:too_short:1"]
            assert short[0].detail == {
                "runtime_seconds": ONE_MINUTE.total_seconds(),
                "min_runtime_seconds": (2 * ONE_MINUTE).total_seconds(),
            }
            assert ok.run_id in short[0].message

        run_monitor(h, body)

    def test_lost_alert_after_restart(self, tmp_path: Path) -> None:
        """Raise one ``lost`` alert for an attempt whose minder is gone after a restart."""
        h = monitored(tmp_path, "jobs:\n  a: {command: x, schedule: manual}\n")
        a = make_run("a", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[a], attempts=[make_attempt(h, a, pid=dead_pid())]))

        async def body(monitor: AlertMonitor) -> None:
            """Check the run was lost, and alerted once."""
            assert h.run("a").reason == "lost"
            lost = await raised(h, monitor, AlertKind.LOST)
            assert [x.alert_id for x in lost] == [f"{a.run_id}:lost:1"]
            assert a.run_id in lost[0].message

        run_monitor(h, body)


NEVER_STARTED = """\
calendars:
  uk: {weekdays: [mon, tue, wed, thu, fri]}
jobs:
  feed: {command: x, schedule: manual, calendar: uk, run_date: previous_business_day}
  rep:
    command: x
    schedule: "0 2 * * *"
    calendar: uk
    run_date: previous_business_day
    must_start_by: "03:00"
    depends_on: [feed]
"""
"""``rep`` fires at 02:00 for the previous business day and waits on ``feed`` for ever."""


class TestNeverStarted:
    """A run not started by its ``must_start_by`` deadline raises ``never_started``."""

    def test_never_started_uses_firing_day(self, tmp_path: Path) -> None:
        """Alert at 03:00 on the firing day (Monday), not on the run date (Friday)."""
        h = monitored(tmp_path, NEVER_STARTED)

        async def body(monitor: AlertMonitor) -> None:
            """Watch the run wait past 03:00."""
            await step(h, monitor, START + TWO_HOURS)
            run = h.run("rep", TODAY - timedelta(days=3))
            await step(h, monitor, START + timedelta(hours=2, minutes=59))
            assert await raised(h, monitor) == []
            await step(h, monitor, START + timedelta(hours=3))
            alerts = await raised(h, monitor)
            assert [a.alert_id for a in alerts] == [f"{run.run_id}:never_started:0"]
            assert alerts[0].message == f"{run.run_id} has not started by 03:00 UTC"

        run_monitor(h, body)

    def test_never_started_duration(self, tmp_path: Path) -> None:
        """Alert a held run at ``scheduled_at + 30m``; a run started at +10m never alerts."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              up: {command: x, schedule: manual}
              a: {command: x, schedule: manual, must_start_by: 30m, depends_on: [up]}
              b: {command: x, schedule: manual, must_start_by: 30m, depends_on: [up]}
            """,
        )
        h.launcher.plan("up", FakeResult(duration=10 * ONE_MINUTE))

        async def body(monitor: AlertMonitor) -> None:
            """Hold ``a``, let ``b`` start once ``up`` ends, and check at 30 minutes."""
            a = await h.engine.trigger("a", actor=ACTOR)
            await h.engine.trigger("b", actor=ACTOR)
            await h.engine.hold(a.run_id, actor=ACTOR)
            await h.engine.trigger("up", actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            assert h.run("b").state is RunState.RUNNING
            await step(h, monitor, START + 30 * ONE_MINUTE - timedelta(seconds=1))
            assert await raised(h, monitor) == []
            await step(h, monitor, START + 30 * ONE_MINUTE)
            assert h.run("a").state is RunState.HELD
            alerts = await raised(h, monitor)
            assert [x.alert_id for x in alerts] == [f"{a.run_id}:never_started:0"]
            await step(h, monitor, START + TWO_HOURS)
            assert len(await raised(h, monitor)) == ONE

        run_monitor(h, body)

    def test_backfill_raises_no_never_started(self, tmp_path: Path) -> None:
        """Raise no ``never_started`` for a backfill over the 30 days before today."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              feed: {command: x, schedule: manual}
              rep:
                command: x
                schedule: "0 2 * * *"
                must_start_by: "03:00"
                depends_on: [feed]
            """,
        )

        async def body(monitor: AlertMonitor) -> None:
            """Backfill a month of waiting runs and find no alert."""
            start = TODAY - timedelta(days=30)
            runs = await h.engine.backfill(
                "rep", start=start, end=TODAY - timedelta(days=1), actor=ACTOR
            )
            assert runs
            await step(h, monitor, START + timedelta(hours=1))
            assert await raised(h, monitor, AlertKind.NEVER_STARTED) == []

        run_monitor(h, body)

    def test_restart_arms_deadlines(self, tmp_path: Path) -> None:
        """Alert at once after a restart past the deadline; a second restart adds nothing."""
        h = monitored(tmp_path, NEVER_STARTED)
        run_ids: list[str] = []

        async def before(monitor: AlertMonitor) -> None:
            """Let the run be created, then stop before 03:00."""
            await step(h, monitor, START + timedelta(hours=2, minutes=30))
            run_ids.append(h.run("rep", TODAY - timedelta(days=3)).run_id)
            assert await raised(h, monitor) == []

        run_monitor(h, before)
        again = restarted(h)

        async def after(monitor: AlertMonitor) -> None:
            """Find the alert raised at start."""
            alerts = await raised(again, monitor, AlertKind.NEVER_STARTED)
            assert [a.alert_id for a in alerts] == [f"{run_ids[0]}:never_started:0"]
            assert alerts[0].raised_at == START + timedelta(hours=4)

        run_monitor(again, after, at=START + timedelta(hours=4))
        third = restarted(again)

        async def last(monitor: AlertMonitor) -> None:
            """Find still just the one alert."""
            await step(third, monitor, START + timedelta(hours=6))
            assert len(await raised(third, monitor)) == ONE

        run_monitor(third, last, at=START + timedelta(hours=5))

    def test_timer_error_is_logged_not_fail_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Log an error inside a monitor timer; the engine carries on and the job ends."""
        h = monitored(tmp_path, "jobs:\n  slow: {command: x, schedule: manual, max_runtime: 5m}\n")
        h.launcher.plan("slow", FakeResult(duration=7 * ONE_MINUTE))

        def broken(*_: object) -> deadlines.Wording:
            """Fail like a bug in the wording would."""
            msg = "wording bug"
            raise RuntimeError(msg)

        monkeypatch.setattr(deadlines, "too_long", broken)

        async def body(monitor: AlertMonitor) -> None:
            """Run the job past its max_runtime and to its end."""
            await h.engine.trigger("slow", actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            assert h.engine.error is None
            assert h.run("slow").state is RunState.SUCCEEDED
            assert await raised(h, monitor) == []

        run_monitor(h, body)


class TestSchedulerAlerts:
    """Scheduler-wide alerts, store trouble, and keeping jobs on time."""

    def test_reload_failed_alert(self, tmp_path: Path) -> None:
        """Store a ``reload_failed`` alert with the problem redacted."""
        h = monitored(
            tmp_path,
            'jobs:\n  a: {command: x, schedule: manual, env: {T: "${TOKEN}"}}\n',
            env={"TOKEN": HIDDEN},
        )
        problem = Problem("pysched.yaml", 3, f"bad token {HIDDEN}", job="a")

        async def body(monitor: AlertMonitor) -> None:
            """Raise the alert and read it back."""
            monitor.reload_failed([problem])
            (alert,) = await raised(h, monitor)
            assert alert_topic(alert) == "alert.reload_failed.high._scheduler"
            assert alert.criticality is Criticality.HIGH
            assert alert.run_id is None
            assert alert.message == "reload failed: 1 error(s); nothing changed"
            assert alert.alert_id == f"_scheduler:reload_failed:{START:%Y%m%dT%H%M%S%fZ}"
            assert alert.detail == {
                "problems": [
                    {
                        "file": "pysched.yaml",
                        "line": 3,
                        "message": "bad token ${TOKEN}",
                        "job": "a",
                        "severity": "error",
                    }
                ]
            }

        run_monitor(h, body)

    def test_store_write_failure_is_fail_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail the engine stop when an alert cannot be written, and raise nothing more."""
        h = monitored(tmp_path, FAILING.replace("{count: 1, delay: 1m}", "{count: 0}"))
        h.launcher.plan("f", FakeResult(exit_code=FAILED_EXIT))
        tries: list[Batch] = []
        save = h.engine.writer.save

        async def broken(batch: Batch) -> None:
            """Fail every alert write; pass the engine's own writes through."""
            if batch.alerts:
                tries.append(batch)
                msg = "disk full"
                raise OSError(msg)
            await save(batch)

        async def body(monitor: AlertMonitor) -> None:
            """Fail the job, wait for the fail-stop, then try another alert."""
            monkeypatch.setattr(h.engine.writer, "save", broken)
            await h.engine.trigger("f", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            error = await pynet.loop.wait_for(h.engine.wait_failed(), WAIT_FAILED_SECONDS)
            assert str(error) == "disk full"
            monitor.reload_failed([Problem("pysched.yaml", 1, "oops")])
            await monitor.settle()
            assert len(tries) == ONE

        run_monitor(h, body)

    def test_alert_does_not_delay_jobs(self, tmp_path: Path) -> None:
        """Start the downstream at the upstream's end although the notifier takes 2 s."""
        h = monitored(
            tmp_path,
            """\
            jobs:
              up: {command: x, schedule: manual}
              down: {command: x, depends_on: [{job: up, on: failure}]}
            """,
        )
        h.launcher.plan("up", FakeResult(exit_code=FAILED_EXIT))

        async def body(monitor: AlertMonitor) -> None:
            """Fail the upstream and check the downstream started at once."""
            await h.engine.trigger("up", actor=ACTOR)
            await step(h, monitor, START + ONE_MINUTE)
            down = h.run("down")
            assert down.state is RunState.RUNNING
            latest = await pynet.loop.run_in_thread(h.store.latest_attempts, [down.run_id])
            assert latest[down.run_id].started_at == START + ONE_MINUTE
            assert len(await raised(h, monitor, AlertKind.FAILED)) == ONE

        slow = RecordingNotifier(delay=SLOW_NOTIFIER_SECONDS)
        run_monitor(h, body, notifier=slow)
        assert len(slow.received) == ONE  # it arrived, long after the downstream started
