"""Test attempts: launch specs, outcomes, retries with backoff, and timeouts."""

import signal
from datetime import datetime, timedelta
from pathlib import Path

import pynet.loop
import pytest
from engine_helpers import START, Harness, MakeHarness

from pyscheduler.engine.launcher import LaunchSpec
from pyscheduler.engine.model import Attempt, Outcome, RunState
from pyscheduler.engine.runbook import OVERLAP_REASON
from pyscheduler.testing import FakeHandle, FakeResult

ONE_MINUTE = timedelta(minutes=1)
AT_0100 = START.replace(hour=1)
RUN_ID = "r.2026-10-19.1"
THREE = 3
TWO = 2
FIVE_MINUTES = timedelta(minutes=5)
GRACE = timedelta(seconds=10)
FOUR_HOURS = timedelta(hours=4)


def job(body: str) -> str:
    """Return a config with one job ``r`` at 01:00 UTC and the extra ``body`` lines."""
    lines = "".join(f"    {line}\n" for line in body.strip().splitlines())
    return f"jobs:\n  r:\n    schedule: '0 1 * * *'\n{lines}"


async def attempts(h: Harness) -> list[Attempt]:
    """Read the attempts of ``r``'s first run from the store, off the loop thread."""
    return await pynet.loop.run_in_thread(h.store.attempts_for, RUN_ID)


class TestRetries:
    """Failed attempts are retried with growing waits, then the run fails."""

    def test_retry_backoff_delays(self, harness: MakeHarness) -> None:
        """Fail, fail, succeed: start at 0, then 1m after the end, then 2m after."""
        h = harness(job("command: work\nretries: {count: 2, delay: 1m, backoff: 2}"))
        h.launcher.plan("r", FakeResult(exit_code=1), FakeResult(exit_code=1), FakeResult())

        async def body() -> None:
            """Run all three attempts."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            assert h.run("r").state is RunState.RETRY_WAIT
            await h.advance_to(AT_0100 + timedelta(minutes=10))
            rows = await attempts(h)
            starts = [row.started_at for row in rows]
            assert starts == [
                AT_0100,
                AT_0100 + timedelta(minutes=2),
                AT_0100 + timedelta(minutes=5),
            ]
            assert [row.outcome for row in rows] == [
                Outcome.FAILED,
                Outcome.FAILED,
                Outcome.SUCCEEDED,
            ]
            assert rows[0].reason == "exit 1"

        h.run_engine(body)
        run = h.run("r")
        assert run.state is RunState.SUCCEEDED
        assert run.attempt == THREE

    def test_start_error_retries(self, harness: MakeHarness) -> None:
        """Retry a start error, then fail with its message as the reason."""
        h = harness(job("command: missing\nretries: {count: 1, delay: 1m}"))
        h.launcher.plan("r", FakeResult(start_error="No such file"))

        async def body() -> None:
            """Let both attempts fail to start."""
            await h.advance_to(AT_0100 + timedelta(minutes=5))
            rows = await attempts(h)
            assert [row.outcome for row in rows] == [Outcome.START_ERROR] * TWO

        h.run_engine(body)
        run = h.run("r")
        assert run.state is RunState.FAILED
        assert "No such file" in run.reason
        assert run.reason.startswith("start error: ")

    def test_launch_oserror_is_start_error(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Treat a launcher that raises ``OSError`` as a start error."""
        h = harness(job("command: work"))

        async def launch(spec: LaunchSpec) -> FakeHandle:
            """Refuse to start anything."""
            raise OSError(spec.run_id)

        monkeypatch.setattr(h.launcher, "launch", launch)

        async def body() -> None:
            """Fire the job."""
            await h.advance_to(AT_0100)
            rows = await attempts(h)
            assert rows[0].outcome is Outcome.START_ERROR
            assert rows[0].pid is None

        h.run_engine(body)
        assert h.run("r").reason == f"start error: {RUN_ID}"

    def test_signal_reason_and_kill_flag(self, harness: MakeHarness) -> None:
        """Name the signal in the reason; an operator kill never retries."""
        h = harness(job("command: work\nretries: {count: 3}"))
        h.launcher.plan(
            "r", FakeResult(signal=int(signal.SIGKILL)), FakeResult(duration=FOUR_HOURS)
        )

        async def body() -> None:
            """Fail once by signal, then kill the retry by operator flag."""
            await h.advance_to(AT_0100 + ONE_MINUTE)
            assert h.run("r").reason.startswith("retry 1/3 after signal 9")
            await h.advance(ONE_MINUTE)
            assert h.run("r").state is RunState.RUNNING
            h.engine._kill_requested.add(RUN_ID)
            h.launcher.running[RUN_ID].kill()
            await h.engine.settle()
            rows = await attempts(h)
            assert rows[-1].outcome is Outcome.KILLED

        h.run_engine(body)
        assert h.run("r").state is RunState.FAILED
        assert h.run("r").reason == "killed"


class TestTimeouts:
    """A job over its timeout is asked to stop, then killed."""

    def test_timeout_terminates_then_kills(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Send TERM at +5m, KILL at +5m10s, and record ``timeout``."""
        h = harness(job("command: stubborn\ntimeout: 5m"))
        h.launcher.plan("r", FakeResult(duration=FOUR_HOURS, ignores_term=True))
        calls: list[tuple[str, datetime]] = []
        real_terminate = FakeHandle.terminate
        real_kill = FakeHandle.kill

        def terminate(handle: FakeHandle) -> None:
            """Note the time, then terminate."""
            calls.append(("terminate", h.clock.now()))
            real_terminate(handle)

        def kill(handle: FakeHandle) -> None:
            """Note the time, then kill."""
            calls.append(("kill", h.clock.now()))
            real_kill(handle)

        monkeypatch.setattr(FakeHandle, "terminate", terminate)
        monkeypatch.setattr(FakeHandle, "kill", kill)

        async def body() -> None:
            """Run past the timeout and the grace."""
            await h.advance_to(AT_0100 + FIVE_MINUTES)
            assert h.run("r").state is RunState.RUNNING
            await h.advance_to(AT_0100 + FIVE_MINUTES + GRACE)
            rows = await attempts(h)
            assert rows[0].outcome is Outcome.TIMEOUT
            assert rows[0].signal == signal.SIGKILL

        h.run_engine(body)
        assert calls == [
            ("terminate", AT_0100 + FIVE_MINUTES),
            ("kill", AT_0100 + FIVE_MINUTES + GRACE),
        ]
        assert h.run("r").state is RunState.FAILED
        assert h.run("r").reason == "timeout"


class TestLaunchSpec:
    """The launch spec carries the rendered command, paths and environment."""

    def test_string_command_and_default_logs(self, harness: MakeHarness) -> None:
        """Run a string through ``/bin/sh -c`` with templates filled in."""
        h = harness(job("command: echo {run_date} {attempt}\nenv: {COLOUR: blue}"))

        async def body() -> None:
            """Fire the job."""
            await h.advance_to(AT_0100)

        h.run_engine(body)
        spec = h.launcher.launched[0]
        assert spec.argv == ("/bin/sh", "-c", "echo 2026-10-19 1")
        assert spec.env["COLOUR"] == "blue"
        assert spec.env["PYSCHED_JOB"] == "r"
        assert spec.env["PYSCHED_RUN_DATE"] == "2026-10-19"
        assert spec.env["PYSCHED_RUN_ID"] == RUN_ID
        assert spec.env["PYSCHED_ATTEMPT"] == "1"
        logs = h.tmp_path / "logs" / "r" / "2026-10-19"
        assert spec.stdout_path == logs / f"{RUN_ID}.1.out"
        assert spec.stderr_path == logs / f"{RUN_ID}.1.err"
        assert spec.status_path == h.tmp_path / "state" / "status" / f"{RUN_ID}.1.json"

    def test_list_command_and_merged_stderr(self, harness: MakeHarness) -> None:
        """Render each word of a list command, and merge stderr into stdout."""
        h = harness(job('command: [report, "--id", "{run_id}"]\nstderr: stdout\ncwd: /tmp\n'))

        async def body() -> None:
            """Fire the job and check the attempt row."""
            await h.advance_to(AT_0100)
            rows = await attempts(h)
            assert rows[0].stderr_path == rows[0].stdout_path
            assert rows[0].pid is not None

        h.run_engine(body)
        spec = h.launcher.launched[0]
        assert spec.argv == ("report", "--id", RUN_ID)
        assert spec.stderr_path is None
        assert spec.cwd == "/tmp"  # noqa: S108 -- just a path in the config
        assert isinstance(spec.stdout_path, Path)


class TestRetryOverlap:
    """A retry obeys ``overlap: queue`` like any run that becomes ready."""

    def test_retry_waits_for_active_run(self, harness: MakeHarness) -> None:
        """Never run two attempts of one job at once, even when one is a retry."""
        h = harness(
            """
            jobs:
              q:
                command: tick
                schedule: {every: 15m, between: "00:00-01:00"}
                retries: {count: 1, delay: 10m}
            """
        )
        half_hour = timedelta(minutes=30)
        h.launcher.plan(
            "q",
            FakeResult(duration=timedelta(minutes=10), exit_code=1),
            FakeResult(duration=half_hour),
        )
        first = "q.2026-10-19.1"

        async def body() -> None:
            """Fail the first run while a second one is running, then go on."""
            await h.advance_to(START + timedelta(minutes=20))
            retrying = h.engine.book.get(first)
            assert retrying is not None
            assert retrying.state is RunState.WAITING
            assert retrying.reason == OVERLAP_REASON
            await h.advance_to(START + timedelta(hours=3))

        h.run_engine(body)
        assert h.launcher.max_concurrent == 1
        done = h.engine.book.get(first)
        assert done is not None
        assert done.state is RunState.SUCCEEDED
        assert done.attempt == TWO
