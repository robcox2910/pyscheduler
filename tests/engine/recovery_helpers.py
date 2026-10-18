"""Shared helpers of the recovery tests: seed a store, restart an engine, run a body."""

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pynet.loop
from engine_helpers import START, TODAY, Harness, TestBody

from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Outcome, Run, RunState, Trigger, make_run_id
from pyscheduler.store import Batch, Store
from pyscheduler.testing import FakeLauncher


def make_run(
    job: str,
    state: RunState,
    *,
    run_date: date = TODAY,
    seq: int = 1,
    scheduled_at: datetime = START,
    attempt: int = 0,
    trigger: Trigger = Trigger.SCHEDULE,
) -> Run:
    """Return a stored-looking run of ``job``."""
    return Run(
        run_id=make_run_id(job, run_date, seq),
        job=job,
        run_date=run_date,
        scheduled_at=scheduled_at,
        state=state,
        reason="",
        attempt=attempt,
        rerun=0,
        trigger=trigger,
        config_version=1,
        created_at=scheduled_at,
        updated_at=scheduled_at,
    )


def status_path(h: Harness, run_id: str, attempt: int = 1) -> Path:
    """Return where the minder would write the note of ``run_id``'s attempt."""
    return h.tmp_path / "state" / "status" / f"{run_id}.{attempt}.json"


def make_attempt(
    h: Harness,
    run: Run,
    *,
    pid: int | None,
    started_at: datetime = START,
    ended_at: datetime | None = None,
    outcome: Outcome | None = None,
) -> Attempt:
    """Return the attempt row of ``run``'s latest attempt."""
    return Attempt(
        run_id=run.run_id,
        attempt=run.attempt,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=None,
        signal=None,
        outcome=outcome,
        pid=pid,
        stdout_path=str(h.tmp_path / "out"),
        stderr_path=str(h.tmp_path / "out"),
        status_path=str(status_path(h, run.run_id, run.attempt)),
        reason="",
    )


def write_status(path: Path, *, exit_code: int = 0, ended_at: datetime = START) -> None:
    """Write a minder status note that says the job exited with ``exit_code``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    note = {
        "exit_code": exit_code,
        "signal": None,
        "started_at": ended_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "start_error": None,
    }
    path.write_text(json.dumps(note), encoding="utf-8")


def note_pid(h: Harness, run: Run, pid: int) -> None:
    """Write the minder's ``.pid`` file for ``run``, started (really) just now."""
    path = status_path(h, run.run_id, run.attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    note = {"pid": pid, "started_at": datetime.now(UTC).isoformat()}
    path.with_name(f"{path.name}.pid").write_text(json.dumps(note), encoding="utf-8")


def seed(h: Harness, batch: Batch) -> None:
    """Commit ``batch`` to the harness store before the engine starts."""
    h.store.apply(batch)


def dead_pid() -> int:
    """Return the pid of a process that has exited and been reaped."""
    process = subprocess.Popen(["true"])  # noqa: S607 -- a harmless system command
    process.wait()
    return process.pid


def run_at(h: Harness, at: datetime, body: TestBody) -> None:
    """Move the (not yet started) engine's clock to ``at``, then start it and run ``body``."""

    async def main() -> None:
        """Advance, start, settle, run the body, stop."""
        await h.clock.advance_to(at)
        await h.engine.start()
        try:
            await h.engine.settle()
            await body()
        finally:
            await h.engine.stop()

    pynet.loop.run(main())


def restarted(h: Harness) -> Harness:
    """Return a new harness on the same state folder, its clock where the old one stopped."""
    store = Store.open(h.tmp_path / "state", sync=False)
    clock = FakeClock(h.clock.now())
    launcher = FakeLauncher(clock)
    engine = Engine(h.config, store, clock=clock, launcher=launcher)
    return Harness(h.tmp_path, h.config, store, clock, launcher, engine)
