"""Test recovery after a restart: finishing, adopting and losing attempts, and re-queuing."""

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Generator
from datetime import timedelta

import pynet.loop
import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness
from recovery_helpers import (
    dead_pid,
    make_attempt,
    make_run,
    note_pid,
    run_at,
    seed,
    status_path,
    write_status,
)

from pyscheduler.engine import recovery
from pyscheduler.engine.adopted import pid_alive
from pyscheduler.engine.model import Event, Outcome, RunState, Trigger
from pyscheduler.store import Batch

ONE_MINUTE = timedelta(minutes=1)
HALF_MINUTE = timedelta(seconds=30)
POLL = timedelta(seconds=recovery.ADOPT_POLL)
REAL_NAP = 0.02
REAL_DEADLINE = 5.0
SECOND_ATTEMPT = 2
OLD_SEQ = 4
LONG_AGO = TODAY - timedelta(days=200)

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

MANUAL = """
jobs:
  q:
    command: run-q
    schedule: manual
  r:
    command: run-r
    schedule: manual
    retries: {count: 2, delay: 1m}
  t:
    command: run-t
    schedule: manual
    timeout: 1m
"""


@pytest.fixture
def sleeper() -> Generator[subprocess.Popen[bytes]]:
    """Start a ``sleep 30`` in its own session, like a minder.

    It is our child, so the engine's ``waitpid`` check clears it away once
    it ends (no zombie that looks alive).
    """
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)  # noqa: S607
    try:
        yield process
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(process.pid, 0)


async def until_final(h: Harness, run_id: str) -> None:
    """Advance the fake clock a poll at a time until ``run_id`` is final (real deadline)."""
    deadline = time.monotonic() + REAL_DEADLINE
    while time.monotonic() < deadline:
        run = h.engine.book.get(run_id)
        if run is not None and run.is_final:
            return
        await pynet.loop.sleep(REAL_NAP)  # let the signalled process really end
        await h.advance(POLL)
    msg = f"{run_id} never ended"
    raise AssertionError(msg)


class TestRunningRuns:
    """A run that was running when the scheduler stopped is finished, adopted or lost."""

    def test_recover_finishes_from_status(self, harness: MakeHarness) -> None:
        """Finish ``a`` from its status note, then start its downstream ``b``."""
        h = harness(CHAIN)
        a = make_run("a", RunState.RUNNING, attempt=1)
        b = make_run("b", RunState.WAITING, trigger=Trigger.DEPENDENCY)
        seed(h, Batch(runs=[a, b], attempts=[make_attempt(h, a, pid=dead_pid())]))
        write_status(status_path(h, a.run_id))

        async def body() -> None:
            """Check the run finished and ``b`` was launched."""
            assert h.run("a").state is RunState.SUCCEEDED
            assert [spec.run_id for spec in h.launcher.launched] == [b.run_id]
            assert h.engine.admission.running == 1  # only b

        h.run_engine(body)
        with h.reopened() as store:
            assert [x.outcome for x in store.attempts_for(a.run_id)] == [Outcome.SUCCEEDED]

    def test_recover_lost_retries(self, harness: MakeHarness) -> None:
        """Mark an attempt with no note and a dead pid lost, then retry it."""
        h = harness(CHAIN)
        a = make_run("a", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[a], attempts=[make_attempt(h, a, pid=dead_pid())]))

        async def body() -> None:
            """Check the lost attempt and the retry."""
            assert h.run("a").state is RunState.RETRY_WAIT
            assert h.run("a").reason == "retry 1/1 after lost"
            attempts = await pynet.loop.run_in_thread(h.store.attempts_for, a.run_id)
            assert [x.outcome for x in attempts] == [Outcome.LOST]
            await h.advance(ONE_MINUTE)
            assert h.run("a").attempt == SECOND_ATTEMPT
            await h.advance(ONE_MINUTE)
            assert h.run("a").state is RunState.SUCCEEDED

        h.run_engine(body)

    def test_missing_attempt_row_or_pid_is_lost(self, harness: MakeHarness) -> None:
        """Lose a running run with no attempt row, and one with no pid and no pid file."""
        h = harness(MANUAL)
        q = make_run("q", RunState.RUNNING, attempt=1)
        t = make_run("t", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[q, t], attempts=[make_attempt(h, t, pid=None)]))

        async def body() -> None:
            """Check both runs failed as lost."""
            for run_id in (q.run_id, t.run_id):
                run = h.engine.book.get(run_id)
                assert run is not None
                assert run.state is RunState.FAILED
                assert run.reason == "lost"
            attempts = await pynet.loop.run_in_thread(h.store.attempts_for, q.run_id)
            assert [(x.attempt, x.outcome) for x in attempts] == [(1, Outcome.LOST)]

        h.run_engine(body)

    def test_adopts_live_pid(self, harness: MakeHarness, sleeper: subprocess.Popen[bytes]) -> None:
        """Adopt a live process: it keeps its slot until its note appears."""
        h = harness(MANUAL)
        q = make_run("q", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[q], attempts=[make_attempt(h, q, pid=sleeper.pid)]))
        note_pid(h, q, sleeper.pid)

        async def body() -> None:
            """Check the run is adopted, then finished by its note."""
            await h.advance(POLL)
            assert h.run("q").state is RunState.RUNNING
            assert q.run_id in h.engine._handles
            assert h.engine.admission.running == 1
            write_status(status_path(h, q.run_id))
            await h.advance(POLL)
            assert h.run("q").state is RunState.SUCCEEDED
            assert h.engine.admission.running == 0

        h.run_engine(body)

    def test_adopts_pid_from_pid_file(
        self, harness: MakeHarness, sleeper: subprocess.Popen[bytes]
    ) -> None:
        """Adopt a process whose pid only reached the minder's ``.pid`` file."""
        h = harness(MANUAL)
        q = make_run("q", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[q], attempts=[make_attempt(h, q, pid=None)]))
        note_pid(h, q, sleeper.pid)

        async def body() -> None:
            """Check the run is adopted and its pid recorded."""
            assert h.run("q").state is RunState.RUNNING
            await h.engine.settle()
            attempts = await pynet.loop.run_in_thread(h.store.attempts_for, q.run_id)
            assert attempts[0].pid == sleeper.pid
            events = await pynet.loop.run_in_thread(h.store.events_for, q.run_id)
            assert [e.action for e in events] == ["attempt_adopted"]

        h.run_engine(body)

    def test_adopted_timeout_rearmed_from_start(
        self, harness: MakeHarness, sleeper: subprocess.Popen[bytes]
    ) -> None:
        """Time out an adopted attempt whose timeout passed while the scheduler was down."""
        h = harness(MANUAL)
        t = make_run("t", RunState.RUNNING, attempt=1)
        row = make_attempt(h, t, pid=sleeper.pid, started_at=START - 5 * ONE_MINUTE)
        seed(h, Batch(runs=[t], attempts=[row]))
        note_pid(h, t, sleeper.pid)

        async def body() -> None:
            """Wait for the group to be signalled and the run to fail."""
            await until_final(h, t.run_id)
            assert h.run("t").state is RunState.FAILED
            assert h.run("t").reason == "timeout"

        h.run_engine(body)

    def test_adopted_kill(self, harness: MakeHarness, sleeper: subprocess.Popen[bytes]) -> None:
        """Kill an adopted attempt through its process group."""
        h = harness(MANUAL)
        q = make_run("q", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[q], attempts=[make_attempt(h, q, pid=sleeper.pid)]))
        note_pid(h, q, sleeper.pid)

        async def body() -> None:
            """Kill the run and wait for it to end."""
            await h.engine.kill(q.run_id, actor="alice")
            await until_final(h, q.run_id)
            assert h.run("q").reason == "killed by alice"

        h.run_engine(body)

    def test_stop_killing_adopted(
        self, harness: MakeHarness, sleeper: subprocess.Popen[bytes]
    ) -> None:
        """End an adopted attempt at once as killed when stopping with ``kill_running``."""
        h = harness(MANUAL)
        q = make_run("q", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[q], attempts=[make_attempt(h, q, pid=sleeper.pid)]))
        note_pid(h, q, sleeper.pid)

        async def main() -> None:
            """Start, then stop killing everything."""
            await h.engine.start()
            await h.engine.settle()
            await h.engine.stop(kill_running=True)

        pynet.loop.run(main())
        assert h.run("q").state is RunState.FAILED
        assert h.run("q").reason == "killed"
        assert not pid_alive(sleeper.pid)

    @pytest.mark.parametrize("adopt", [False, True])
    def test_kill_before_restart_still_counts(
        self, harness: MakeHarness, sleeper: subprocess.Popen[bytes], *, adopt: bool
    ) -> None:
        """End an attempt killed before the restart as killed, with no retry."""
        h = harness(MANUAL)
        r = make_run("r", RunState.RUNNING, attempt=1)
        pid = sleeper.pid if adopt else dead_pid()
        kill = Event(START + ONE_MINUTE, "alice", r.run_id, "kill", "{}")
        seed(h, Batch(runs=[r], attempts=[make_attempt(h, r, pid=pid)], events=[kill]))
        if adopt:
            note_pid(h, r, pid)

        async def body() -> None:
            """Check the run failed as killed by alice (the adopted one is signalled again)."""
            await until_final(h, r.run_id)
            assert h.run("r").state is RunState.FAILED
            assert h.run("r").reason == "killed by alice"
            attempts = await pynet.loop.run_in_thread(h.store.attempts_for, r.run_id)
            assert [a.outcome for a in attempts] == [Outcome.KILLED]

        run_at(h, START + 2 * ONE_MINUTE, body)

    def test_stranger_with_the_pid_is_lost(
        self, harness: MakeHarness, sleeper: subprocess.Popen[bytes]
    ) -> None:
        """Lose an attempt whose pid now belongs to a process started at another time."""
        h = harness(MANUAL)
        q = make_run("q", RunState.RUNNING, attempt=1)
        seed(h, Batch(runs=[q], attempts=[make_attempt(h, q, pid=sleeper.pid)]))
        path = status_path(h, q.run_id)
        path.parent.mkdir(parents=True)
        note = {"pid": sleeper.pid, "started_at": "2020-01-01T00:00:00+00:00"}
        path.with_name(f"{path.name}.pid").write_text(json.dumps(note))

        async def body() -> None:
            """Check the run was lost and the stranger left alone."""
            assert h.run("q").state is RunState.FAILED
            assert h.run("q").reason == "lost"
            assert sleeper.poll() is None

        h.run_engine(body)


class TestOtherOpenRuns:
    """Queued, retry-waiting, held and orphaned runs after a restart."""

    def test_requeue_and_rearm_retry(self, harness: MakeHarness) -> None:
        """Start a queued run at once; retry a retry-waiting run when its delay is up."""
        h = harness(MANUAL)
        q = make_run("q", RunState.QUEUED)
        r = make_run("r", RunState.RETRY_WAIT, attempt=1)
        ended = START - HALF_MINUTE
        row = make_attempt(h, r, pid=None, ended_at=ended, outcome=Outcome.FAILED)
        seed(h, Batch(runs=[q, r], attempts=[row]))

        async def body() -> None:
            """Check the launch order and times."""
            assert [s.run_id for s in h.launcher.launched] == [q.run_id]
            await h.advance(HALF_MINUTE - timedelta(seconds=1))
            assert h.run("r").state is RunState.RETRY_WAIT
            await h.advance(timedelta(seconds=1))
            assert [s.run_id for s in h.launcher.launched] == [q.run_id, r.run_id]
            assert h.run("r").attempt == SECOND_ATTEMPT

        h.run_engine(body)

    def test_held_stays_and_orphans_cancelled(self, harness: MakeHarness) -> None:
        """Keep a held run held; cancel the open run of a job no longer configured."""
        h = harness(MANUAL)
        held = make_run("q", RunState.HELD)
        gone = make_run("old", RunState.WAITING)
        seed(h, Batch(runs=[held, gone]))

        async def body() -> None:
            """Check both states."""
            assert h.run("q").state is RunState.HELD
            orphan = h.engine.book.get(gone.run_id)
            assert orphan is not None
            assert orphan.state is RunState.CANCELLED
            assert orphan.reason == recovery.GONE

        h.run_engine(body)

    def test_old_open_run_sees_old_upstream(self, harness: MakeHarness) -> None:
        """Load an old open run's date, so its finished upstream lets it start."""
        h = harness(CHAIN)
        day = TODAY - timedelta(days=10)
        a = make_run("a", RunState.SUCCEEDED, run_date=day)
        b = make_run("b", RunState.WAITING, run_date=day, trigger=Trigger.DEPENDENCY)
        seed(h, Batch(runs=[a, b]))

        async def body() -> None:
            """Check ``b`` was launched."""
            assert [spec.run_id for spec in h.launcher.launched] == [b.run_id]

        h.run_engine(body)

    def test_seq_never_reused(self, harness: MakeHarness) -> None:
        """Number new runs after the stored ones, in memory or long ago."""
        h = harness(MANUAL)
        today = make_run("q", RunState.SUCCEEDED)
        old = make_run("q", RunState.SUCCEEDED, run_date=LONG_AGO, seq=OLD_SEQ)
        seed(h, Batch(runs=[today]))

        async def body() -> None:
            """Store a run past retention (not pruned yet), then trigger on both dates."""
            await pynet.loop.run_in_thread(h.store.apply, Batch(runs=[old]))
            assert h.engine.book.published(today.run_id) is not None
            new = await h.engine.trigger("q", actor="alice")
            assert new.run_id == "q.2026-10-19.2"
            back = await h.engine.trigger("q", run_date=LONG_AGO, actor="alice")
            assert back.run_id == f"q.{LONG_AGO.isoformat()}.{OLD_SEQ + 1}"

        h.run_engine(body)

    def test_start_at_later_time(self, harness: MakeHarness) -> None:
        """Recover with a clock that has moved on since the store was written."""
        h = harness(MANUAL)
        r = make_run("r", RunState.RETRY_WAIT, attempt=1)
        row = make_attempt(h, r, pid=None, ended_at=START, outcome=Outcome.FAILED)
        seed(h, Batch(runs=[r], attempts=[row]))

        async def body() -> None:
            """Check the overdue retry started at once."""
            assert h.run("r").attempt == SECOND_ATTEMPT

        run_at(h, START + timedelta(hours=1), body)
