"""Test remote runs across a scheduler restart: re-dispatch, adoption, and loss."""

from datetime import timedelta
from pathlib import Path

from engine_helpers import START
from remote_helpers import remote_harness, restarted, write_config

from pyscheduler.engine.model import Outcome, RunState
from pyscheduler.testing import FakeResult

AT_0100 = START.replace(hour=1)
FIVE_MINUTES = timedelta(minutes=5)
HALF_HOUR = timedelta(minutes=30)
AN_HOUR = timedelta(hours=1)
BUILD = "build-01"
TWO = 2

ONE_JOB = """
jobs:
  build:
    command: make all
    schedule: "0 1 * * *"
    agent: build-01
"""


class TestRestart:
    """A scheduler restart picks remote attempts up again; nothing runs twice."""

    def test_restart_adopts_remote_run(self, tmp_path: Path) -> None:
        """Re-dispatch a running remote attempt after a restart; the agent runs it once."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.plan("build", FakeResult(duration=HALF_HOUR))
        h.hub.connect(BUILD)

        async def first() -> None:
            """Start the run, then stop the engine while it runs."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)
            assert h.run("build").state is RunState.RUNNING

        h.run_engine(first)
        key = h.key("build")
        h2 = restarted(h)

        async def second() -> None:
            """Recover, then let the agent's report finish the run."""
            assert h2.run("build").state is RunState.RUNNING
            assert h2.hub.recovered_calls == 1
            assert h2.busy() == 1
            await h2.advance_to(AT_0100 + AN_HOUR)
            assert h2.run("build").state is RunState.SUCCEEDED
            (row,) = await h2.attempts(key[0])
            assert row.outcome is Outcome.SUCCEEDED
            assert row.stdout_path.endswith(".1.out")

        h2.run_engine(second)
        assert h2.hub.runs[key] == 1
        assert h2.hub.dispatches[key] == TWO
        assert h2.hub.adopted == []
        assert h2.busy() == 0

    def test_restart_adopts_when_job_no_longer_remote(self, tmp_path: Path) -> None:
        """Adopt (not re-dispatch) an attempt whose job is now local, and finish it remotely."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.plan("build", FakeResult(duration=HALF_HOUR))
        h.hub.connect(BUILD)

        async def first() -> None:
            """Start the run, then stop the engine while it runs."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)

        h.run_engine(first)
        key = h.key("build")
        write_config(tmp_path, ONE_JOB.replace("    agent: build-01\n", ""))
        h2 = restarted(h)

        async def second() -> None:
            """Let the agent's report finish the adopted attempt."""
            await h2.advance_to(AT_0100 + AN_HOUR)
            assert h2.run("build").state is RunState.SUCCEEDED

        h2.run_engine(second)
        assert h2.hub.adopted == [key]
        assert h2.hub.dispatches[key] == 1
        assert h2.launcher.launched == []

    def test_restart_without_hub_loses_remote_attempt(self, tmp_path: Path) -> None:
        """With no placement after a restart, a remote attempt cannot be followed: lost."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.plan("build", FakeResult(duration=HALF_HOUR))
        h.hub.connect(BUILD)

        async def first() -> None:
            """Start the run, then stop the engine while it runs."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)

        h.run_engine(first)
        h2 = restarted(h)
        h2.engine.placement = None

        async def second() -> None:
            """Look at the run after recovery."""
            run = h2.run("build")
            assert run.state is RunState.FAILED
            assert run.reason == "lost"

        h2.run_engine(second)

    def test_restart_with_stopped_hub_loses_remote_attempt(self, tmp_path: Path) -> None:
        """A hub that refuses to follow the attempt again after a restart loses it."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.plan("build", FakeResult(duration=HALF_HOUR))
        h.hub.connect(BUILD)

        async def first() -> None:
            """Start the run, then stop the engine while it runs."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)

        h.run_engine(first)
        h2 = restarted(h)
        h2.hub.stopped = True

        async def second() -> None:
            """Look at the run after recovery."""
            assert h2.run("build").reason == "lost"

        h2.run_engine(second)
