"""Test recovery with real processes: a restart and a SIGKILLed scheduler."""

import select
import subprocess
import sys
import time
from pathlib import Path

import pynet.loop
import pytest
from _crash_child import JOBS, READY, write_config

from pyscheduler.config import load_config
from pyscheduler.engine import recovery
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import MinderLauncher
from pyscheduler.engine.model import Outcome, RunState
from pyscheduler.store import Store

CHILD = Path(__file__).with_name("_crash_child.py")
FAST_POLL = 0.25
"""The adopt poll (real seconds) used here, so the tests finish quickly."""

READY_DEADLINE = 6.0
FINISH_DEADLINE = 5.0
NAP = 0.02


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll adopted attempts every ``FAST_POLL`` seconds instead of 2."""
    monkeypatch.setattr(recovery, "ADOPT_POLL", FAST_POLL)


def new_engine(config_path: Path) -> Engine:
    """Return a real engine (wall clock, minders) on the config's state folder."""
    config = load_config(config_path, env={})
    store = Store.open(config.settings.state_dir)
    return Engine(config, store, clock=SystemClock(), launcher=MinderLauncher())


async def wait_until(check: object, deadline: float, what: str) -> None:
    """Nap until ``check()`` is true, failing after ``deadline`` real seconds."""
    assert callable(check)
    end = time.monotonic() + deadline
    while not check():
        if time.monotonic() > end:
            msg = f"timed out waiting for {what}"
            raise AssertionError(msg)
        await pynet.loop.sleep(NAP)


def all_succeeded(engine: Engine, run_ids: list[str]) -> bool:
    """Return whether every run in ``run_ids`` is published as succeeded."""
    runs = [engine.book.published(run_id) for run_id in run_ids]
    return all(run is not None and run.state is RunState.SUCCEEDED for run in runs)


def finish_after_restart(config_path: Path, run_ids: list[str]) -> None:
    """Start a new engine, wait for every run to succeed, and check one attempt each."""
    engine = new_engine(config_path)

    async def main() -> None:
        """Recover and wait for the runs."""
        await engine.start()
        try:
            await wait_until(lambda: all_succeeded(engine, run_ids), FINISH_DEADLINE, "runs")
        finally:
            await engine.stop()

    pynet.loop.run(main())
    store = Store.open(engine.config.settings.state_dir)
    try:
        for run_id in run_ids:
            attempts = store.attempts_for(run_id)
            assert [a.outcome for a in attempts] == [Outcome.SUCCEEDED], run_id
    finally:
        store.close()


def read_ready(child: subprocess.Popen[str]) -> list[str]:
    """Wait (with a deadline) for the child to print READY; return the run ids it gives."""
    assert child.stdout is not None
    end = time.monotonic() + READY_DEADLINE
    while (left := end - time.monotonic()) > 0:
        readable, _, _ = select.select([child.stdout], [], [], left)
        if not readable:
            break
        line = child.stdout.readline()
        if not line:
            break
        words = line.split()
        if words and words[0] == READY:
            return words[1:]
    msg = f"child never said READY (exit {child.poll()})"
    raise AssertionError(msg)


class TestRealRecovery:
    """Real minders survive the scheduler and are picked up again."""

    def test_restart_adopts_live_minder(self, tmp_path: Path) -> None:
        """Stop an engine mid-run; a new engine adopts the minder and sees it succeed."""
        config_path = write_config(tmp_path)
        engine = new_engine(config_path)
        run_ids: list[str] = []

        async def main() -> None:
            """Trigger one job, wait until it runs, stop without killing it."""
            await engine.start()
            try:
                run = await engine.trigger(JOBS[0], actor="test")
                run_ids.append(run.run_id)

                def running() -> bool:
                    """Return whether the run is durably running with a handle."""
                    published = engine.book.published(run.run_id)
                    return (
                        published is not None
                        and published.state is RunState.RUNNING
                        and run.run_id in engine._handles
                    )

                await wait_until(running, FINISH_DEADLINE, "the run to start")
                await engine.settle()
            finally:
                await engine.stop()

        pynet.loop.run(main())
        started = time.monotonic()
        finish_after_restart(config_path, run_ids)
        assert time.monotonic() - started < FINISH_DEADLINE

    def test_sigkill_scheduler_mid_run(self, tmp_path: Path) -> None:
        """SIGKILL a scheduler with 3 running jobs; a new one finishes them, once each."""
        config_path = write_config(tmp_path)
        child = subprocess.Popen(  # noqa: S603 -- our own test script
            [sys.executable, str(CHILD), str(config_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            run_ids = read_ready(child)
        finally:
            child.kill()
            child.wait()
        assert len(run_ids) == len(JOBS)
        finish_after_restart(config_path, run_ids)
