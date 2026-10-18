"""Tests for the engine's attempt listeners (``watch_attempts``)."""

from datetime import timedelta

from engine_helpers import START, MakeHarness

from pyscheduler.engine.model import Attempt, Outcome

MANUAL = """\
jobs:
  q: {command: "true", schedule: manual}
"""

TWO_CALLS = 2
FIVE_MINUTES = timedelta(minutes=5)


class TestWatchAttempts:
    """Attempt listeners hear about each attempt's start and its ending."""

    def test_attempt_hook_sees_start_and_end(self, harness: MakeHarness) -> None:
        """Call the listener twice for one run: once started, once ended."""
        h = harness(MANUAL)
        seen: list[Attempt] = []
        h.engine.watch_attempts(seen.append)

        async def body() -> None:
            """Trigger the job and let it finish."""
            await h.engine.trigger("q", actor="tester")
            await h.advance(FIVE_MINUTES)

        h.run_engine(body)
        assert len(seen) == TWO_CALLS
        started, ended = seen
        assert (started.ended_at, started.started_at) == (None, START)
        assert ended.ended_at is not None
        assert ended.outcome is Outcome.SUCCEEDED

    def test_failing_listener_is_skipped(self, harness: MakeHarness) -> None:
        """Log and skip a listener that raises; the others still hear, and the run ends."""
        h = harness(MANUAL)
        seen: list[Attempt] = []

        def broken(_: Attempt) -> None:
            """Fail every time."""
            msg = "listener bug"
            raise RuntimeError(msg)

        h.engine.watch_attempts(broken)
        h.engine.watch_attempts(seen.append)

        async def body() -> None:
            """Trigger the job and let it finish."""
            run = await h.engine.trigger("q", actor="tester")
            await h.advance(FIVE_MINUTES)
            assert h.run("q").run_id == run.run_id
            assert h.run("q").is_final

        h.run_engine(body)
        assert len(seen) == TWO_CALLS

    def test_unsubscribe(self, harness: MakeHarness) -> None:
        """Stop calling a listener once it is removed (twice is harmless)."""
        h = harness(MANUAL)
        seen: list[Attempt] = []
        stop = h.engine.watch_attempts(seen.append)
        stop()
        stop()

        async def body() -> None:
            """Trigger the job and let it finish."""
            await h.engine.trigger("q", actor="tester")
            await h.advance(FIVE_MINUTES)

        h.run_engine(body)
        assert seen == []
