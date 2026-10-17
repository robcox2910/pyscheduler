"""Tests for the fake launcher that engine tests use instead of real processes."""

import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pynet.loop

from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.launcher import AttemptResult, LaunchSpec
from pyscheduler.testing import FakeLauncher, FakeResult

START = datetime(2026, 10, 19, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
EXIT_ONE = 1
TWO = 2


def spec(run_id: str, attempt: int = 1) -> LaunchSpec:
    """Build a launch spec for ``run_id``."""
    base = Path("/nowhere") / run_id
    return LaunchSpec(
        run_id=run_id,
        attempt=attempt,
        argv=("true",),
        cwd=None,
        env={},
        stdout_path=base.with_suffix(".out"),
        stderr_path=None,
        status_path=base.with_suffix(".json"),
    )


class TestFakeLauncher:
    """The fake launcher finishes attempts when the fake clock says so."""

    def test_fake_launcher_plan(self) -> None:
        """Fail the first launch at +1m, then succeed with the repeated last result."""
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)
        launcher.plan("a", FakeResult(exit_code=EXIT_ONE), FakeResult())
        results: list[AttemptResult] = []

        async def main() -> None:
            """Launch three attempts one after another."""
            for attempt in range(1, 4):
                handle = await launcher.launch(spec("a.2026-10-19.1", attempt))
                assert launcher.running["a.2026-10-19.1"] is handle
                task = pynet.loop.spawn(handle.wait())
                await clock.advance(MINUTE - timedelta(seconds=1))
                assert not task.done()
                await clock.advance(timedelta(seconds=1))
                assert task.done()
                results.append(task.result())
                assert "a.2026-10-19.1" not in launcher.running

        pynet.loop.run(main())
        first, second, third = results
        assert first.exit_code == EXIT_ONE
        assert first.ended_at == START + MINUTE
        assert first.started_at == START
        assert second.succeeded
        assert third.succeeded
        assert [s.attempt for s in launcher.launched] == [1, 2, 3]

    def test_unplanned_job_succeeds_after_a_minute(self) -> None:
        """Default to a one-minute success for a job with no plan."""
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)

        async def main() -> AttemptResult:
            """Launch and advance."""
            handle = await launcher.launch(spec("b.2026-10-19.1"))
            assert handle.pid is not None
            task = pynet.loop.spawn(handle.wait())
            await clock.advance(MINUTE)
            return task.result()

        result = pynet.loop.run(main())
        assert result.succeeded
        assert result.ended_at == START + MINUTE

    def test_start_error_fails_at_launch(self) -> None:
        """Finish a start-error attempt at once, never running."""
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)
        launcher.plan("c", FakeResult(start_error="no such program"))

        async def main() -> AttemptResult:
            """Launch and wait without advancing."""
            handle = await launcher.launch(spec("c.2026-10-19.1"))
            assert "c.2026-10-19.1" not in launcher.running
            return await handle.wait()

        result = pynet.loop.run(main())
        assert result.start_error == "no such program"
        assert result.exit_code is None
        assert result.ended_at == START

    def test_terminate_and_kill(self) -> None:
        """End at once with 15 on terminate, unless it ignores TERM; 9 on kill."""
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)
        launcher.plan("stubborn", FakeResult(ignores_term=True))

        async def main() -> tuple[AttemptResult, AttemptResult]:
            """Terminate one polite and one stubborn attempt."""
            polite = await launcher.launch(spec("polite.2026-10-19.1"))
            stubborn = await launcher.launch(spec("stubborn.2026-10-19.1"))
            assert launcher.max_concurrent == TWO
            await clock.advance(timedelta(seconds=10))
            polite.terminate()
            stubborn.terminate()
            await pynet.loop.sleep(0)
            assert "stubborn.2026-10-19.1" in launcher.running
            stubborn.kill()
            polite.kill()  # already finished: no effect
            await clock.advance(MINUTE)
            return await polite.wait(), await stubborn.wait()

        polite, stubborn = pynet.loop.run(main())
        assert polite.signal == signal.SIGTERM
        assert polite.exit_code is None
        assert polite.ended_at == START + timedelta(seconds=10)
        assert stubborn.signal == signal.SIGKILL
        assert launcher.running == {}

    def test_signal_result(self) -> None:
        """Finish with a planned signal."""
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)
        launcher.plan("d", FakeResult(duration=timedelta(seconds=5), exit_code=None, signal=6))

        async def main() -> AttemptResult:
            """Launch and advance past the duration."""
            handle = await launcher.launch(spec("d.2026-10-19.1"))
            await clock.advance(timedelta(seconds=5))
            return await handle.wait()

        assert pynet.loop.run(main()).signal == signal.SIGABRT
