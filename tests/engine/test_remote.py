"""Test remote runs: placement on agents, queued reasons, liveness, restarts and slots."""

import signal
from datetime import datetime, timedelta
from pathlib import Path

from engine_helpers import START
from remote_helpers import remote_harness

from pyscheduler.engine.model import Outcome, RunState
from pyscheduler.engine.remote import NO_PLACEMENT
from pyscheduler.testing import FakeResult

AT_0100 = START.replace(hour=1)
ONE_MINUTE = timedelta(minutes=1)
FIVE_MINUTES = timedelta(minutes=5)
AN_HOUR = timedelta(hours=1)
KILL_GRACE = timedelta(seconds=10)
BUILD = "build-01"
FIRST_SLOT_ONLY = 1
TWO = 2
EXIT_ONE = 1
PYSCHED_VARS = ("PYSCHED_JOB", "PYSCHED_RUN_DATE", "PYSCHED_RUN_ID", "PYSCHED_ATTEMPT")

ONE_JOB = """
jobs:
  build:
    command: make all
    schedule: "0 1 * * *"
    agent: build-01
    env: {TARGET: "{run_date}"}
"""

TAGGED = """
jobs:
  train:
    command: train
    schedule: "0 1 * * *"
    agent_tags: [gpu]
"""

TWO_JOBS = """
jobs:
  a:
    command: run-a
    schedule: "0 1 * * *"
    agent: build-01
  b:
    command: run-b
    schedule: "0 1 * * *"
    agent: build-01
"""


def at(minutes: int, *, hour: int = 1) -> datetime:
    """Return ``hour``:``minutes`` on the first day."""
    return START.replace(hour=hour, minute=minutes)


class TestRemoteRuns:
    """Agent jobs run on agents through the hub, not through the launcher."""

    def test_remote_run_succeeds(self, tmp_path: Path) -> None:
        """Run ``agent: build-01`` there; the row names the agent and the agent's log paths."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.connect(BUILD)

        async def body() -> None:
            """Let the run start and finish."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)
            run = h.run("build")
            assert run.state is RunState.SUCCEEDED
            (row,) = await h.attempts(run.run_id)
            assert row.status_path == "agent:build-01"
            assert row.pid is None
            assert row.outcome is Outcome.SUCCEEDED
            assert row.stdout_path == f"/agent/build-01/{run.run_id}.1.out"
            assert row.stderr_path == f"/agent/build-01/{run.run_id}.1.err"

        h.run_engine(body)
        assert h.launcher.launched == []
        (payload,) = h.hub.payloads
        assert payload["argv"] == ["/bin/sh", "-c", "make all"]
        env = payload["env"]
        assert isinstance(env, dict)
        assert env["TARGET"] == "2026-10-19"
        assert env["PYSCHED_ATTEMPT"] == "1"
        assert set(env) == {"TARGET", *PYSCHED_VARS}  # never the scheduler's own environment
        assert h.hub.dispatches[h.key("build")] == 1

    def test_started_paths_written_while_running(self, tmp_path: Path) -> None:
        """Write the agent-side log paths on the attempt row as soon as ``started`` arrives."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.plan("build", FakeResult(duration=AN_HOUR))
        h.hub.connect(BUILD)

        async def body() -> None:
            """Look at the row while the attempt runs."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            run = h.run("build")
            assert run.state is RunState.RUNNING
            (row,) = await h.attempts(run.run_id)
            assert row.stdout_path.endswith(".1.out")
            assert row.pid is None
            assert row.ended_at is None

        h.run_engine(body)


class TestPlacement:
    """Runs wait, with a reason, until an agent fits; caps and pools still apply."""

    def test_waits_for_tagged_agent(self, tmp_path: Path) -> None:
        """Queue a gpu job with a reason, then start it when a gpu agent connects."""
        h = remote_harness(tmp_path, TAGGED)
        h.hub.connect("cpu-01", tags=["linux"])

        async def body() -> None:
            """Wait, connect the gpu agent, then finish."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)
            run = h.run("train")
            assert run.state is RunState.QUEUED
            assert run.reason == "waiting for an agent with tags gpu"
            assert h.busy() == 0
            h.hub.connect("gpu-01", tags=["gpu", "linux"])
            await h.engine.settle()
            assert h.run("train").state is RunState.RUNNING
            assert h.run("train").reason == ""
            await h.advance(FIVE_MINUTES)
            assert h.run("train").state is RunState.SUCCEEDED

        h.run_engine(body)
        assert [str(p["job"]) for p in h.hub.payloads] == ["train"]

    def test_offline_agent_reason(self, tmp_path: Path) -> None:
        """Queue a run for a named agent that never connected, saying it is offline."""
        h = remote_harness(tmp_path, ONE_JOB)

        async def body() -> None:
            """Look at the queued run."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            assert h.run("build").reason == "waiting for agent build-01 (offline)"

        h.run_engine(body)

    def test_no_placement_reason(self, tmp_path: Path) -> None:
        """With no hub at all, an agent job waits and says so; nothing runs locally."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.engine.placement = None

        async def body() -> None:
            """Look at the queued run."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            assert h.run("build").reason == NO_PLACEMENT

        h.run_engine(body)
        assert h.launcher.launched == []

    def test_busy_agent_queues_then_starts(self, tmp_path: Path) -> None:
        """With one slot on the agent, the second run waits (busy) and starts after the first."""
        h = remote_harness(tmp_path, TWO_JOBS)
        h.hub.connect(BUILD, max_running=FIRST_SLOT_ONLY)
        h.hub.plan("a", FakeResult(duration=FIVE_MINUTES))
        h.hub.plan("b", FakeResult(duration=FIVE_MINUTES))

        async def body() -> None:
            """Check both runs along the way."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            first, second = h.run("a"), h.run("b")
            assert first.state is RunState.RUNNING
            assert second.state is RunState.QUEUED
            assert second.reason == "waiting for agent build-01 (busy)"
            await h.advance_to(AT_0100 + FIVE_MINUTES)
            assert h.run("b").state is RunState.RUNNING
            assert h.busy() == FIRST_SLOT_ONLY
            await h.advance(FIVE_MINUTES)
            assert h.run("b").state is RunState.SUCCEEDED

        h.run_engine(body)

    def test_global_cap_still_applies(self, tmp_path: Path) -> None:
        """A full global cap holds the second run back, and it takes no agent slot meanwhile."""
        h = remote_harness(tmp_path, TWO_JOBS, settings="max_running: 1")
        h.hub.connect(BUILD)
        h.hub.plan("a", FakeResult(duration=FIVE_MINUTES))

        async def body() -> None:
            """Check the agent's load while one run waits for the cap."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            assert h.run("a").state is RunState.RUNNING
            assert h.run("b").state is RunState.QUEUED
            assert h.busy() == FIRST_SLOT_ONLY
            await h.advance_to(AT_0100 + timedelta(minutes=10))
            assert h.run("b").state is RunState.SUCCEEDED

        h.run_engine(body)

    def test_pool_checked_before_placement(self, tmp_path: Path) -> None:
        """A run waiting for its pool takes no agent slot."""
        text = TWO_JOBS.replace("agent: build-01", "agent: build-01\n    pools: {db: 1}")
        h = remote_harness(tmp_path, "pools: {db: 1}\n" + text)
        h.hub.connect(BUILD)
        h.hub.plan("a", FakeResult(duration=FIVE_MINUTES))

        async def body() -> None:
            """Check the agent's load while one run waits for the pool."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            assert h.busy() == FIRST_SLOT_ONLY
            assert h.run("b").state is RunState.QUEUED

        h.run_engine(body)


class TestLiveness:
    """Timeouts, reconnects and lost agents."""

    def test_timeout_sends_kill(self, tmp_path: Path) -> None:
        """At the timeout send TERM; one ignored is followed by KILL after the grace."""
        h = remote_harness(tmp_path, ONE_JOB.replace("agent:", "timeout: 5m\n    agent:"))
        h.hub.plan("build", FakeResult(duration=AN_HOUR, ignores_term=True))
        h.hub.connect(BUILD)

        async def body() -> None:
            """Ring the timeout and the kill."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)
            key = h.key("build")
            assert h.hub.signals == [(key, "TERM")]
            await h.advance(KILL_GRACE)
            assert h.hub.signals == [(key, "TERM"), (key, "KILL")]
            run = h.run("build")
            assert run.state is RunState.FAILED
            (row,) = await h.attempts(run.run_id)
            assert row.outcome is Outcome.TIMEOUT
            assert row.signal == int(signal.SIGKILL)

        h.run_engine(body)

    def test_reconnect_reports_finish(self, tmp_path: Path) -> None:
        """An agent offline when its job ends reports it on reconnect: succeeded, not lost."""
        h = remote_harness(tmp_path, ONE_JOB)
        h.hub.plan("build", FakeResult(duration=FIVE_MINUTES))
        h.hub.connect(BUILD)

        async def body() -> None:
            """Drop the agent mid-run, then bring it back inside ``lost_after``."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            h.hub.disconnect(BUILD)
            await h.advance_to(AT_0100 + timedelta(minutes=8))
            assert not h.hub.online(BUILD)
            assert h.run("build").state is RunState.RUNNING
            h.hub.connect(BUILD)
            await h.engine.settle()
            assert h.run("build").state is RunState.SUCCEEDED

        h.run_engine(body)
        assert h.hub.runs[h.key("build")] == 1

    def test_lost_after(self, tmp_path: Path) -> None:
        """No report within ``lost_after`` of going offline: lost, retried if retries remain."""
        text = TWO_JOBS.replace("command: run-b", "command: run-b\n    retries: {count: 1}")
        h = remote_harness(tmp_path, text)
        h.hub.plan("a", FakeResult(duration=AN_HOUR))
        h.hub.plan("b", FakeResult(duration=AN_HOUR), FakeResult(duration=ONE_MINUTE))
        h.hub.connect(BUILD)

        async def body() -> None:
            """Silence the agent past the grace and ``lost_after``, then bring it back."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            h.hub.disconnect(BUILD)
            await h.advance_to(AT_0100 + timedelta(minutes=20))
            a, b = h.run("a"), h.run("b")
            assert a.state is RunState.FAILED
            assert a.reason == "lost"
            (row,) = await h.attempts(a.run_id)
            assert row.outcome is Outcome.LOST
            assert b.state is RunState.QUEUED
            assert b.reason == "waiting for agent build-01 (offline)"
            assert h.busy() == 0
            h.hub.connect(BUILD)
            await h.advance(FIVE_MINUTES)
            assert h.run("b").state is RunState.SUCCEEDED
            assert h.run("b").attempt == TWO

        h.run_engine(body)


class TestSlots:
    """The agent slot comes back on every way an attempt can end."""

    def test_slot_released_on_every_path(self, tmp_path: Path) -> None:
        """Success, failure, timeout, kill, a refused dispatch and a lost agent all free it."""
        text = """
            jobs:
              ok: {command: ok, schedule: "0 1 * * *", agent: build-01}
              bad: {command: bad, schedule: "0 1 * * *", agent: build-01}
              slow: {command: slow, schedule: "0 1 * * *", agent: build-01, timeout: 5m}
              stopped: {command: stopped, schedule: "0 2 * * *", agent: build-01}
              killed: {command: killed, schedule: "0 3 * * *", agent: build-01}
              gone: {command: gone, schedule: "0 4 * * *", agent: build-01}
            """
        h = remote_harness(tmp_path, text)
        h.hub.plan("bad", FakeResult(exit_code=EXIT_ONE))
        for name in ("slow", "killed", "gone"):
            h.hub.plan(name, FakeResult(duration=AN_HOUR))
        h.hub.connect(BUILD)

        async def body() -> None:
            """Drive each ending in turn and check the agent's load after it."""
            await h.advance_to(at(10))
            assert [h.run(n).state for n in ("ok", "bad", "slow")] == [
                RunState.SUCCEEDED,
                RunState.FAILED,
                RunState.FAILED,
            ]
            assert h.busy() == 0
            h.hub.stopped = True
            await h.advance_to(at(1, hour=2))
            assert h.run("stopped").state is RunState.FAILED
            assert h.run("stopped").reason.startswith("start error: ")
            assert h.busy() == 0
            h.hub.stopped = False
            await h.advance_to(at(1, hour=3))
            await h.engine.kill(h.run("killed").run_id, actor="alice")
            await h.advance(ONE_MINUTE)
            assert h.run("killed").state is RunState.FAILED
            assert h.busy() == 0
            await h.advance_to(at(1, hour=4))
            assert h.busy() == 1
            h.hub.disconnect(BUILD)
            await h.advance_to(at(30, hour=4))
            assert h.run("gone").reason == "lost"
            assert h.busy() == 0

        h.run_engine(body)
