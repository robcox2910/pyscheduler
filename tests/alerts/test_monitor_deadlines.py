"""Tests for ``never_started`` deadlines that are already past when a run is heard."""

from datetime import timedelta
from pathlib import Path

from engine_helpers import START
from monitor_helpers import monitored, raised, reconfigured, run_monitor, step
from recovery_helpers import restarted

from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.config.model import AlertKind
from pyscheduler.engine.model import RunState
from pyscheduler.testing import FakeResult

ACTOR = "tester"
ONE_MINUTE = timedelta(minutes=1)
TWO_HOURS = timedelta(hours=2)
FAILED_EXIT = 1

WAITING = """\
jobs:
  up: {command: x, schedule: manual}
  a: {command: x, schedule: manual, must_start_by: 30m, depends_on: [up]}
"""

DOWNSTREAM = """\
jobs:
  up: {command: x, schedule: manual}
  a: {command: x, must_start_by: 30m, depends_on: [up]}
  b: {command: x, must_start_by: 30m, depends_on: [up]}
"""


class TestPastDeadlines:
    """A run heard live rings ``never_started`` only for a deadline still ahead."""

    def test_rerun_of_a_skipped_run_is_not_late(self, tmp_path: Path) -> None:
        """Raise nothing when a run skipped before it started is rerun long after its deadline."""
        h = monitored(tmp_path, WAITING)

        async def body(monitor: AlertMonitor) -> None:
            """Skip ``a`` before its deadline, then rerun it two hours later."""
            run = await h.engine.trigger("a", actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            await h.engine.skip(run.run_id, actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS)
            await h.engine.rerun(run.run_id, actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS + ONE_MINUTE)
            assert h.run("a").state is RunState.WAITING
            assert await raised(h, monitor, AlertKind.NEVER_STARTED) == []

        run_monitor(h, body)

    def test_rerun_downstream_is_not_late(self, tmp_path: Path) -> None:
        """Raise nothing for downstream runs reopened by ``rerun --downstream`` past their deadline."""
        h = monitored(tmp_path, DOWNSTREAM)
        h.launcher.plan("up", FakeResult(exit_code=FAILED_EXIT), FakeResult(duration=TWO_HOURS))

        async def body(monitor: AlertMonitor) -> None:
            """Fail ``up``, skip ``a`` and ``b``, then rerun everything two hours later."""
            run = await h.engine.trigger("up", actor=ACTOR)
            await step(h, monitor, START + 5 * ONE_MINUTE)
            for job in ("a", "b"):
                await h.engine.skip(h.run(job).run_id, actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS)
            reopened = await h.engine.rerun(run.run_id, downstream=True, actor=ACTOR)
            assert sorted(r.job for r in reopened) == ["a", "b", "up"]
            await step(h, monitor, START + TWO_HOURS + ONE_MINUTE)
            assert h.run("a").state is RunState.WAITING
            assert await raised(h, monitor, AlertKind.NEVER_STARTED) == []

        run_monitor(h, body)

    def test_reload_adding_must_start_by_is_not_late(self, tmp_path: Path) -> None:
        """Raise nothing for old waiting runs when a reload adds a deadline already past."""
        h = monitored(tmp_path, WAITING.replace(", must_start_by: 30m", ""))

        async def body(monitor: AlertMonitor) -> None:
            """Leave ``a`` waiting for two hours, then add ``must_start_by: 30m``."""
            await h.engine.trigger("a", actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS)
            await h.engine.reload(reconfigured(tmp_path, WAITING), actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS + ONE_MINUTE)
            assert h.run("a").state is RunState.WAITING
            assert await raised(h, monitor, AlertKind.NEVER_STARTED) == []

        run_monitor(h, body)

    def test_restart_after_a_late_rerun_is_not_late(self, tmp_path: Path) -> None:
        """Raise nothing at a restart for a deadline that passed while the scheduler was up."""
        h = monitored(tmp_path, WAITING)

        async def before(monitor: AlertMonitor) -> None:
            """Skip ``a``, then rerun it after its deadline, and stop."""
            run = await h.engine.trigger("a", actor=ACTOR)
            await step(h, monitor, START + 10 * ONE_MINUTE)
            await h.engine.skip(run.run_id, actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS)
            await h.engine.rerun(run.run_id, actor=ACTOR)
            await step(h, monitor, START + TWO_HOURS + ONE_MINUTE)

        run_monitor(h, before)
        again = restarted(h)

        async def after(monitor: AlertMonitor) -> None:
            """Find no ``never_started`` alert after the restart."""
            assert again.run("a").state is RunState.WAITING
            assert await raised(again, monitor, AlertKind.NEVER_STARTED) == []

        run_monitor(again, after, at=START + 2 * TWO_HOURS)
