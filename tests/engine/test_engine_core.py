"""Test the engine core: firings, dependencies, overlap, pools and write-ahead."""

import threading
from datetime import date, timedelta

import pynet.loop
import pytest
from engine_helpers import START, TODAY, MakeHarness

from pyscheduler.engine.launcher import LaunchSpec
from pyscheduler.engine.model import Run, RunState, Trigger
from pyscheduler.engine.runbook import OVERLAP_REASON
from pyscheduler.store import Batch
from pyscheduler.testing import FakeHandle, FakeResult

ONE_MINUTE = timedelta(minutes=1)
AT_1830 = START.replace(hour=18, minute=30)
TUESDAY = date(2026, 10, 20)
POOL_LIMIT = 2
POOL_JOBS = 5
MANY_JOBS = 200
MANY_CAP = 4
GATE_TIMEOUT = 5.0
TURNS = 20
LONG = timedelta(minutes=50)
FOUR_HOURS = timedelta(hours=4)

CHAIN = """
jobs:
  a:
    command: run-a
    schedule: "30 18 * * *"
  b:
    command: run-b
    depends_on: [a]
"""


def ids(specs: list[LaunchSpec]) -> list[str]:
    """Return the run ids of launched specs, in launch order."""
    return [spec.run_id for spec in specs]


class TestDependencies:
    """Runs wait for, block on and skip because of their upstreams."""

    def test_scheduled_chain_runs_in_order(self, harness: MakeHarness) -> None:
        """Run ``a`` at 18:30, then ``b`` once ``a`` has succeeded."""
        h = harness(CHAIN)

        async def body() -> None:
            """Walk the clock through both runs."""
            await h.advance_to(AT_1830)
            assert ids(h.launcher.launched) == ["a.2026-10-19.1"]
            assert h.run("b").state is RunState.WAITING
            await h.advance(ONE_MINUTE)
            assert h.run("a").state is RunState.SUCCEEDED
            assert ids(h.launcher.launched) == ["a.2026-10-19.1", "b.2026-10-19.1"]
            await h.advance(ONE_MINUTE)
            assert h.run("b").state is RunState.SUCCEEDED

        h.run_engine(body)
        assert h.run("a").trigger is Trigger.SCHEDULE
        assert h.run("b").trigger is Trigger.DEPENDENCY
        assert h.run("b").scheduled_at == AT_1830

    def test_blocked_then_not_failed(self, harness: MakeHarness) -> None:
        """Block ``b`` (not fail it) when ``a`` fails with no retries left."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(exit_code=3))

        async def body() -> None:
            """Let ``a`` fail."""
            await h.advance_to(AT_1830 + ONE_MINUTE)

        h.run_engine(body)
        assert h.run("a").state is RunState.FAILED
        assert h.run("a").reason == "exit 3"
        assert h.run("b").state is RunState.BLOCKED
        assert "blocked: a" in h.run("b").reason

    def test_on_failure_skipped(self, harness: MakeHarness) -> None:
        """Skip a cleanup job that runs only on failure when ``a`` succeeds."""
        h = harness(
            """
            jobs:
              a:
                command: run-a
                schedule: "30 18 * * *"
              cleanup:
                command: tidy
                depends_on: [{job: a, on: failure}]
            """
        )

        async def body() -> None:
            """Let ``a`` succeed."""
            await h.advance_to(AT_1830 + ONE_MINUTE)

        h.run_engine(body)
        assert h.run("cleanup").state is RunState.SKIPPED
        assert ids(h.launcher.launched) == ["a.2026-10-19.1"]

    def test_dependency_only_chain_and_offset(self, harness: MakeHarness) -> None:
        """Create dependency-only runs transitively, with offsets applied in reverse."""
        h = harness(
            """
            jobs:
              a:
                command: run-a
                schedule: "30 18 * * *"
              b:
                command: run-b
                depends_on: [a]
              c:
                command: run-c
                depends_on: [b]
              d:
                command: run-d
                depends_on: [{job: a, run_date: -1}]
            """
        )

        async def body() -> None:
            """Fire ``a`` and let everything finish."""
            await h.advance_to(AT_1830 + timedelta(minutes=5))

        h.run_engine(body)
        assert h.run("c").trigger is Trigger.DEPENDENCY
        assert h.run("c").state is RunState.SUCCEEDED
        assert h.runs("d") == []
        assert h.run("d", TUESDAY).state is RunState.SUCCEEDED
        assert h.run("d", TUESDAY).scheduled_at == AT_1830


class TestOverlap:
    """A job's runs queue, skip or overlap as configured."""

    def test_overlap_queue_supersedes_same_run_date(self, harness: MakeHarness) -> None:
        """Keep only the newest overlap-waiting run of one run date."""
        h = harness(
            """
            jobs:
              q:
                command: tick
                schedule: {every: 15m, between: "00:00-00:30"}
            """
        )
        h.launcher.plan("q", FakeResult(duration=LONG), FakeResult())

        async def body() -> None:
            """Fire three times while the first run is still going."""
            assert ids(h.launcher.launched) == ["q.2026-10-19.1"]
            await h.advance(timedelta(minutes=15))
            second = h.runs("q")[1]
            assert second.state is RunState.WAITING
            assert second.reason.startswith(OVERLAP_REASON)
            await h.advance(timedelta(minutes=15))
            first, second, third = h.runs("q")
            assert first.state is RunState.RUNNING
            assert second.state is RunState.CANCELLED
            assert second.reason == "superseded"
            assert third.state is RunState.WAITING
            await h.advance_to(START + LONG)
            assert h.runs("q")[2].state is RunState.RUNNING
            assert ids(h.launcher.launched) == ["q.2026-10-19.1", "q.2026-10-19.3"]

        h.run_engine(body)

    def test_overlap_other_run_date_never_superseded(self, harness: MakeHarness) -> None:
        """Let a waiting Monday run survive a Tuesday firing, and run them in order."""
        h = harness(
            """
            jobs:
              x:
                command: crunch
                schedule: ["0 22,23 * * *", "30 0 * * *"]
            """
        )
        h.launcher.plan("x", FakeResult(duration=FOUR_HOURS))
        tuesday = START + timedelta(days=1)

        async def body() -> None:
            """Fire Monday 22:00 and 23:00, then Tuesday 00:30."""
            await h.advance_to(tuesday.replace(minute=30))
            monday = h.runs("x")
            assert [run.state for run in monday[1:]] == [RunState.RUNNING, RunState.WAITING]
            assert h.run("x", TUESDAY).state is RunState.WAITING
            await h.advance_to(tuesday.replace(hour=2))
            assert h.runs("x")[2].state is RunState.RUNNING
            assert h.run("x", TUESDAY).state is RunState.WAITING
            await h.advance_to(tuesday.replace(hour=6))
            assert h.run("x", TUESDAY).state is RunState.RUNNING

        h.run_engine(body)

    def test_overlap_skip(self, harness: MakeHarness) -> None:
        """Cancel a firing that arrives while the job is active, with ``overlap: skip``."""
        h = harness(
            """
            jobs:
              s:
                command: tick
                schedule: {every: 15m, between: "00:00-00:15"}
                overlap: skip
            """
        )
        h.launcher.plan("s", FakeResult(duration=timedelta(minutes=20)))

        async def body() -> None:
            """Fire twice during one long run."""
            await h.advance(timedelta(minutes=15))

        h.run_engine(body)
        first, second = h.runs("s")
        assert first.state is RunState.RUNNING
        assert second.state is RunState.CANCELLED
        assert second.reason == "skipped: overlap"


class TestAdmission:
    """Pools and the global cap limit how much runs at once."""

    def test_pool_limits_concurrency(self, harness: MakeHarness) -> None:
        """Never run more than two jobs that each need one of two ``db`` slots."""
        jobs = "".join(
            f"  p{n}:\n    command: q\n    schedule: '30 18 * * *'\n    pools: {{db: 1}}\n"
            for n in range(POOL_JOBS)
        )
        h = harness(f"pools:\n  db: {POOL_LIMIT}\njobs:\n{jobs}")

        async def body() -> None:
            """Step minute by minute, checking the running count."""
            await h.advance_to(AT_1830)
            for _ in range(POOL_JOBS):
                assert len(h.launcher.running) <= POOL_LIMIT
                await h.advance(ONE_MINUTE)

        h.run_engine(body)
        assert h.launcher.max_concurrent == POOL_LIMIT
        assert all(h.run(f"p{n}").state is RunState.SUCCEEDED for n in range(POOL_JOBS))

    def test_many_queued_runs_finish(self, harness: MakeHarness) -> None:
        """Push 200 runs through a cap of four."""
        jobs = "".join(
            f"  j{n}:\n    command: q\n    schedule: '30 18 * * *'\n" for n in range(MANY_JOBS)
        )
        h = harness(f"jobs:\n{jobs}", settings=f"max_running: {MANY_CAP}")

        async def body() -> None:
            """Run until every job is done."""
            await h.advance_to(AT_1830 + ONE_MINUTE * (MANY_JOBS // MANY_CAP))

        h.run_engine(body)
        assert h.launcher.max_concurrent == MANY_CAP
        assert len(h.launcher.launched) == MANY_JOBS
        assert all(h.run(f"j{n}").state is RunState.SUCCEEDED for n in range(MANY_JOBS))


class TestWriteAhead:
    """Nothing is acted on or shown before it is durable."""

    def test_write_ahead(self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        """Commit every ``running`` state before its launch, and notify after commit."""
        h = harness(CHAIN)
        log: list[tuple[str, str, RunState | None]] = []
        real_apply = h.store.apply
        real_launch = h.launcher.launch

        def apply(batch: Batch) -> None:
            """Commit, then note every run state that is now durable."""
            real_apply(batch)
            log.extend(("apply", run.run_id, run.state) for run in batch.runs)

        async def launch(spec: LaunchSpec) -> FakeHandle:
            """Note the launch, then really launch."""
            log.append(("launch", spec.run_id, None))
            return await real_launch(spec)

        monkeypatch.setattr(h.store, "apply", apply)
        monkeypatch.setattr(h.launcher, "launch", launch)
        seen: list[tuple[str, RunState]] = []

        def notified(run: Run) -> None:
            """Check the run is already published and durable."""
            assert h.engine.book.published(run.run_id) == run
            assert ("apply", run.run_id, run.state) in log
            seen.append((run.run_id, run.state))

        h.engine.subscribe(notified)

        async def body() -> None:
            """Run the chain to the end."""
            await h.advance_to(AT_1830 + timedelta(minutes=2))

        h.run_engine(body)
        launches = [i for i, entry in enumerate(log) if entry[0] == "launch"]
        assert len(launches) == len(["a", "b"])
        for index in launches:
            run_id = log[index][1]
            assert ("apply", run_id, RunState.RUNNING) in log[:index]
        assert ("b.2026-10-19.1", RunState.SUCCEEDED) in seen

    def test_published_view_lags_until_durable(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Show the old published state while the commit is stuck."""
        h = harness(CHAIN)
        gate = threading.Event()
        gate.set()
        real_apply = h.store.apply

        def gated(batch: Batch) -> None:
            """Wait for the gate, then commit."""
            assert gate.wait(GATE_TIMEOUT)
            real_apply(batch)

        monkeypatch.setattr(h.store, "apply", gated)
        run_id = "a.2026-10-19.1"

        async def body() -> None:
            """Block the commit of ``a``'s ending and look at both views."""
            await h.advance_to(AT_1830)
            gate.clear()
            try:
                await h.clock.advance(ONE_MINUTE)
                for _ in range(TURNS):
                    await pynet.loop.sleep(0)
                assert h.run("a").state is RunState.SUCCEEDED
                published = h.engine.book.published(run_id)
                assert published is not None
                assert published.state is RunState.RUNNING
            finally:
                gate.set()
            await h.engine.settle()
            published = h.engine.book.published(run_id)
            assert published is not None
            assert published.state is RunState.SUCCEEDED

        h.run_engine(body)


class TestEngineLifecycle:
    """Start, stop, subscribe and the firing bookkeeping."""

    def test_last_tick_and_events_saved(self, harness: MakeHarness) -> None:
        """Save ``last_tick`` with the firing, and the run's logbook lines."""
        h = harness(CHAIN)

        async def body() -> None:
            """Run ``a`` and read the store from a worker thread."""
            await h.advance_to(AT_1830 + ONE_MINUTE)
            tick = await pynet.loop.run_in_thread(h.store.get_engine, "last_tick")
            assert tick == AT_1830.isoformat()
            events = await pynet.loop.run_in_thread(h.store.events_for, "a.2026-10-19.1")
            actions = [event.action for event in events]
            assert actions == ["created", "attempt_started", "attempt_ended", "finished"]
            assert all(event.actor == "scheduler" for event in events)

        h.run_engine(body)

    def test_unsubscribe_stops_notifications(self, harness: MakeHarness) -> None:
        """Stop calling a subscriber once it unsubscribes."""
        h = harness(CHAIN)
        seen: list[Run] = []
        unsubscribe = h.engine.subscribe(seen.append)

        async def body() -> None:
            """Fire ``a``, unsubscribe, then let it finish."""
            await h.advance_to(AT_1830)
            unsubscribe()
            count = len(seen)
            await h.advance(ONE_MINUTE)
            assert len(seen) == count

        h.run_engine(body)
        assert seen

    def test_subscriber_error_does_not_stop_engine(self, harness: MakeHarness) -> None:
        """Keep going when a subscriber raises."""
        h = harness(CHAIN)

        def broken(run: Run) -> None:
            """Raise for every run."""
            raise RuntimeError(run.run_id)

        h.engine.subscribe(broken)

        async def body() -> None:
            """Run the chain."""
            await h.advance_to(AT_1830 + timedelta(minutes=2))

        h.run_engine(body)
        assert h.run("b").state is RunState.SUCCEEDED

    def test_stop_stops_firing(self, harness: MakeHarness) -> None:
        """Create no runs after stop, and allow stopping twice."""
        h = harness(CHAIN)

        async def body() -> None:
            """Stop, then move past the firing."""
            await h.engine.stop()
            await h.clock.advance_to(AT_1830)

        h.run_engine(body)
        assert h.runs("a") == []

    def test_stop_kill_running(self, harness: MakeHarness) -> None:
        """Kill running attempts on ``stop(kill_running=True)`` and record them killed."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Start ``a``, then stop with kill."""
            await h.advance_to(AT_1830)
            await h.engine.stop(kill_running=True)

        h.run_engine(body)
        assert h.run("a").state is RunState.FAILED
        assert h.run("a").reason == "killed"
        assert h.launcher.running == {}

    def test_stop_leaves_running_minders(self, harness: MakeHarness) -> None:
        """Leave running attempts alone on a plain stop."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Start ``a``, then stop."""
            await h.advance_to(AT_1830)
            await h.engine.stop()

        h.run_engine(body)
        assert h.run("a").state is RunState.RUNNING
        assert list(h.launcher.running) == ["a.2026-10-19.1"]
        assert h.run("a").run_date == TODAY


class TestCreateRun:
    """The internal ``_create_run`` keeps runs unique and dates right."""

    def test_uniqueness(self, harness: MakeHarness) -> None:
        """Refuse duplicate scheduled and dependency runs; always make manual ones."""
        h = harness(CHAIN)

        async def body() -> None:
            """Fire ``a``, then try to create its runs again."""
            await h.advance_to(AT_1830)
            engine = h.engine
            assert engine._create_run("a", TODAY, AT_1830, Trigger.SCHEDULE) is None
            assert engine._create_run("b", TODAY, AT_1830, Trigger.DEPENDENCY) is None
            manual = engine._create_run("a", TODAY, AT_1830, Trigger.MANUAL)
            assert manual is not None
            assert manual.run_id == "a.2026-10-19.2"
            await engine.settle()

        h.run_engine(body)

    def test_offset_round_trip_must_hold(self, harness: MakeHarness) -> None:
        """Make no downstream run when no run date shifts back onto the upstream's."""
        h = harness(
            """
            calendars:
              weekdays:
                weekdays: [mon, tue, wed, thu, fri]
            jobs:
              a:
                command: run-a
                schedule: manual
                calendar: weekdays
              d:
                command: run-d
                calendar: weekdays
                depends_on: [{job: a, run_date: -1}]
            """
        )
        saturday = date(2026, 10, 24)
        friday = date(2026, 10, 23)

        async def body() -> None:
            """Create ``a`` runs for a Saturday and a Friday."""
            h.engine._create_run("a", saturday, AT_1830, Trigger.BACKFILL)
            h.engine._create_run("a", friday, AT_1830 + ONE_MINUTE, Trigger.BACKFILL)
            await h.engine.settle()

        h.run_engine(body)
        assert h.runs("d", date(2026, 10, 26))[0].trigger is Trigger.DEPENDENCY
        assert len(h.runs("d", date(2026, 10, 26))) == 1

    def test_older_firing_superseded_by_newer_waiting(self, harness: MakeHarness) -> None:
        """Cancel an older run that becomes ready while a newer one already waits."""
        h = harness(
            """
            jobs:
              q:
                command: tick
                schedule: manual
            """
        )
        h.launcher.plan("q", FakeResult(duration=FOUR_HOURS))

        async def body() -> None:
            """Start one run, queue a newer one, then an older one."""
            engine = h.engine
            engine._create_run("q", TODAY, START, Trigger.MANUAL)
            await engine.settle()
            newer = engine._create_run("q", TODAY, AT_1830, Trigger.MANUAL)
            older = engine._create_run("q", TODAY, AT_1830 - ONE_MINUTE, Trigger.MANUAL)
            await engine.settle()
            assert newer is not None
            assert older is not None
            assert h.engine.book.get(newer.run_id) == h.engine.book.latest_for("q", TODAY)
            assert older.state is RunState.CANCELLED
            assert older.reason == "superseded"

        h.run_engine(body)
