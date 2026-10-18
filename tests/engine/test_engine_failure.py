"""Test the fail-stop policy: any failed commit stops the engine cleanly."""

import logging
from datetime import timedelta

import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness

from pyscheduler.engine.model import RunState
from pyscheduler.store import Batch
from pyscheduler.testing import FakeResult

ONE_MINUTE = timedelta(minutes=1)
AT_1830 = START.replace(hour=18, minute=30)
DISK_FULL = "disk full"
BUG = "bug in a timer"

CHAIN = """
jobs:
  a:
    command: run-a
    schedule: "30 18 * * *"
    retries: {count: 1, delay: 1m}
  b:
    command: run-b
    depends_on: [a]
"""


def failing_apply(h: Harness, monkeypatch: pytest.MonkeyPatch, state: RunState | None) -> None:
    """Make every commit fail that carries a run in ``state`` (``None``: the firing batch)."""
    real = h.store.apply

    def apply(batch: Batch) -> None:
        """Raise for the chosen batches, else really commit."""
        if state is None and batch.engine:
            raise OSError(DISK_FULL)
        if state is not None and any(run.state is state for run in batch.runs):
            raise OSError(DISK_FULL)
        real(batch)

    monkeypatch.setattr(h.store, "apply", apply)


def assert_failed_cleanly(h: Harness) -> None:
    """Check the engine recorded the error, holds no slots and runs no attempt tasks."""
    assert isinstance(h.engine.error, OSError)
    assert h.engine.admission.running == 0
    assert h.engine._attempt_tasks == {}
    assert h.engine._firing_task is not None
    assert h.engine._firing_task.done()


class TestFailStop:
    """A failed commit records the error, rolls back, and launches nothing more."""

    def test_firing_batch_fails(
        self,
        harness: MakeHarness,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Forget the runs whose creation never became durable, and stop firing."""
        h = harness(CHAIN)
        failing_apply(h, monkeypatch, None)

        async def body() -> None:
            """Reach the firing, then a day later."""
            await h.advance_to(AT_1830)
            error = await h.engine.wait_failed()
            assert str(error) == DISK_FULL
            await h.advance(timedelta(days=1))

        with caplog.at_level(logging.ERROR, logger="pyscheduler.engine"):
            h.run_engine(body)
        assert_failed_cleanly(h)
        assert h.launcher.launched == []
        # Whatever is left in memory is exactly what is durable (a later
        # group may have made ``running`` durable; recovery calls it lost).
        for run in h.runs("a") + h.runs("b"):
            assert h.engine.book.published(run.run_id) == run
        assert h.runs("a", TODAY + timedelta(days=1)) == []
        assert any("a.2026-10-19.1" in record.getMessage() for record in caplog.records)

    def test_running_commit_fails(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Launch nothing when ``running`` cannot be made durable, and give the slot back."""
        h = harness(CHAIN)
        failing_apply(h, monkeypatch, RunState.RUNNING)

        async def body() -> None:
            """Reach the firing."""
            await h.advance_to(AT_1830)

        h.run_engine(body)
        assert_failed_cleanly(h)
        assert h.launcher.launched == []
        assert h.run("a").state is RunState.QUEUED
        assert h.engine.book.published("a.2026-10-19.1") == h.run("a")

    def test_finish_commit_fails(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Roll the run back to ``running`` and start no downstream run."""
        h = harness(CHAIN)
        failing_apply(h, monkeypatch, RunState.SUCCEEDED)

        async def body() -> None:
            """Let ``a`` finish."""
            await h.advance_to(AT_1830 + ONE_MINUTE)

        h.run_engine(body)
        assert_failed_cleanly(h)
        assert [spec.run_id for spec in h.launcher.launched] == ["a.2026-10-19.1"]
        assert h.run("a").state is RunState.RUNNING
        assert h.run("b").state is RunState.WAITING

    def test_timer_callback_error_fails_engine(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Send an error raised in a retry timer down the same fail path."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(exit_code=1))

        def broken(run_id: str) -> None:
            """Raise like a bug would."""
            msg = f"{BUG} {run_id}"
            raise RuntimeError(msg)

        monkeypatch.setattr(h.engine, "_retry", broken)

        async def body() -> None:
            """Fail ``a`` once and reach its retry time."""
            await h.advance_to(AT_1830 + timedelta(minutes=2))

        h.run_engine(body)
        assert isinstance(h.engine.error, RuntimeError)
        assert BUG in str(h.engine.error)
        assert h.run("a").state is RunState.RETRY_WAIT

    def test_stop_survives_close_errors(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Log, not raise, when closing the store fails."""
        h = harness(CHAIN)
        real_close = h.store.close

        def close() -> None:
            """Close for real, then complain."""
            real_close()
            raise OSError(DISK_FULL)

        monkeypatch.setattr(h.store, "close", close)

        async def body() -> None:
            """Do nothing; stop runs afterwards."""

        h.run_engine(body)
        assert h.engine.error is None
