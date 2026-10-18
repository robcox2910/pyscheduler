"""Tests for the run book: the engine's in-memory index of every run."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from pyscheduler.engine.model import Run, RunState, Trigger, make_run_id
from pyscheduler.engine.runbook import OVERLAP_REASON, RunBook

DAY = date(2026, 10, 19)
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
SEQ_THREE = 3
SEQ_FIVE = 5
EVICTED_COUNT = 2
FAILED_COPIES = 2


def make_run(
    job: str = "a",
    run_date: date = DAY,
    seq: int = 1,
    state: RunState = RunState.SCHEDULED,
    trigger: Trigger = Trigger.SCHEDULE,
    scheduled_at: datetime = T0,
    reason: str = "",
) -> Run:
    """Build a run with sensible defaults."""
    return Run(
        run_id=make_run_id(job, run_date, seq),
        job=job,
        run_date=run_date,
        scheduled_at=scheduled_at,
        state=state,
        reason=reason,
        attempt=0,
        rerun=0,
        trigger=trigger,
        config_version=1,
        created_at=T0,
        updated_at=T0,
    )


class TestRunBook:
    """The book keeps every index in step with the runs put into it."""

    def test_put_replaces_and_reindexes(self) -> None:
        """A state change moves the run between open and final, and active."""
        book = RunBook()
        run = make_run()
        book.put(run)
        assert book.get(run.run_id) == run
        assert book.open_runs() == [run]
        running = replace(run, state=RunState.RUNNING)
        book.put(running)
        assert book.open_runs() == [running]
        assert book.active_for("a") == [running]
        done = replace(run, state=RunState.SUCCEEDED)
        book.put(done)
        assert book.open_runs() == []
        assert book.active_for("a") == []
        assert book.runs_for("a", DAY) == [done]

    def test_put_reindexes_when_keys_change(self) -> None:
        """Changing a keyed field drops the old index entries."""
        book = RunBook()
        run = make_run()
        book.put(run)
        later = T0 + timedelta(hours=1)
        moved = replace(run, scheduled_at=later, run_date=date(2026, 10, 20))
        book.put(moved)
        assert book.runs_for("a", DAY) == []
        assert book.by_scheduled("a", T0) is None
        assert book.by_scheduled("a", later) == moved
        assert book.runs_for("a", date(2026, 10, 20)) == [moved]

    def test_latest_for_picks_latest_scheduled(self) -> None:
        """The latest run is the one with the greatest scheduled time."""
        book = RunBook()
        first = make_run(seq=1)
        second = make_run(seq=2, scheduled_at=T0 + timedelta(hours=2))
        middle = make_run(seq=SEQ_THREE, scheduled_at=T0 + timedelta(hours=1))
        for run in (second, first, middle):
            book.put(run)
        assert book.latest_for("a", DAY) == second
        assert book.runs_for("a", DAY) == [first, middle, second]
        assert book.latest_for("a", date(2026, 1, 1)) is None

    def test_by_scheduled_and_dependency_run(self) -> None:
        """Scheduled-type and dependency runs are indexed separately; manual in neither."""
        book = RunBook()
        sched = make_run(seq=1, trigger=Trigger.SCHEDULE)
        catch = make_run(job="b", trigger=Trigger.CATCH_UP)
        back = make_run(job="c", trigger=Trigger.BACKFILL)
        dep = make_run(job="d", trigger=Trigger.DEPENDENCY)
        manual = make_run(job="e", trigger=Trigger.MANUAL)
        for run in (sched, catch, back, dep, manual):
            book.put(run)
        assert book.by_scheduled("a", T0) == sched
        assert book.by_scheduled("b", T0) == catch
        assert book.by_scheduled("c", T0) == back
        assert book.by_scheduled("d", T0) is None
        assert book.dependency_run("d", DAY) == dep
        assert book.dependency_run("a", DAY) is None
        assert book.by_scheduled("e", T0) is None
        assert book.dependency_run("e", DAY) is None

    def test_overlap_waiting(self) -> None:
        """Find the waiting run whose reason starts with the overlap text."""
        book = RunBook()
        assert book.overlap_waiting("a", DAY) is None
        other = make_run(seq=1, state=RunState.WAITING, reason="waiting for b")
        book.put(other)
        assert book.overlap_waiting("a", DAY) is None
        first = make_run(seq=2, state=RunState.WAITING, reason=OVERLAP_REASON + " (a.2026-10-19.1)")
        book.put(first)
        assert book.overlap_waiting("a", DAY) == first
        later = make_run(
            seq=SEQ_THREE,
            state=RunState.WAITING,
            reason=OVERLAP_REASON,
            scheduled_at=T0 + timedelta(hours=1),
        )
        book.put(later)
        assert book.overlap_waiting("a", DAY) == later
        book.put(replace(later, state=RunState.QUEUED))
        assert book.overlap_waiting("a", DAY) == first

    def test_next_seq_never_reuses(self) -> None:
        """Sequence numbers only go up, even after eviction, and can be seeded."""
        book = RunBook()
        assert book.next_seq("a", DAY) == 1
        book.put(make_run(seq=1))
        book.put(make_run(seq=SEQ_THREE))
        assert book.next_seq("a", DAY) == SEQ_THREE + 1
        book.seed_seq("a", DAY, SEQ_FIVE)
        assert book.next_seq("a", DAY) == SEQ_FIVE + 1
        book.seed_seq("a", DAY, 1)
        assert book.next_seq("a", DAY) == SEQ_FIVE + 1
        book.put(replace(make_run(seq=SEQ_THREE), state=RunState.FAILED))
        book.evict_final_before(date(2027, 1, 1))
        assert book.next_seq("a", DAY) == SEQ_FIVE + 1

    def test_published_separate_from_current(self) -> None:
        """Putting a run does not change the published copy."""
        book = RunBook()
        run = make_run()
        book.put(run)
        assert book.published(run.run_id) is None
        book.publish(run)
        newer = replace(run, state=RunState.RUNNING)
        book.put(newer)
        assert book.published(run.run_id) == run
        assert book.get(run.run_id) == newer
        book.publish(newer)
        assert book.published(run.run_id) == newer

    def test_published_runs_filters_and_sorts(self) -> None:
        """Filter published copies by date, job and state; newest first."""
        book = RunBook()
        old = make_run(job="a", seq=1)
        new = make_run(job="a", seq=2, scheduled_at=T0 + timedelta(hours=1))
        other_day = make_run(job="a", run_date=date(2026, 10, 20), state=RunState.FAILED)
        other_job = make_run(job="b", state=RunState.FAILED)
        for run in (old, new, other_day, other_job):
            book.publish(run)
        everything = book.published_runs()
        assert {r.run_id for r in everything} == {
            old.run_id,
            new.run_id,
            other_day.run_id,
            other_job.run_id,
        }
        assert [r.run_id for r in book.published_runs(run_date=DAY, job="a")] == [
            new.run_id,
            old.run_id,
        ]
        assert book.published_runs(job="b") == [other_job]
        assert book.published_runs(run_date=date(2026, 10, 20)) == [other_day]
        assert {r.run_id for r in book.published_runs(state=RunState.FAILED)} == {
            other_day.run_id,
            other_job.run_id,
        }
        assert book.published_runs(job="zzz") == []
        book.publish(replace(old, job="a", state=RunState.FAILED))
        assert len(book.published_runs(job="a", state=RunState.FAILED)) == FAILED_COPIES

    def test_evict(self) -> None:
        """Eviction drops old final runs from every index and view."""
        book = RunBook()
        old_done = replace(make_run(seq=1), state=RunState.SUCCEEDED)
        old_open = make_run(seq=2, state=RunState.RUNNING, scheduled_at=T0 + timedelta(hours=1))
        new_done = replace(
            make_run(run_date=date(2026, 10, 25), scheduled_at=T0 + timedelta(days=6)),
            state=RunState.SUCCEEDED,
        )
        old_dep = replace(make_run(job="d", trigger=Trigger.DEPENDENCY), state=RunState.FAILED)
        for run in (old_done, old_open, new_done, old_dep):
            book.put(run)
            book.publish(run)
        assert book.evict_final_before(date(2026, 10, 20)) == EVICTED_COUNT
        assert book.get(old_done.run_id) is None
        assert book.published(old_done.run_id) is None
        assert book.dependency_run("d", DAY) is None
        assert book.by_scheduled("a", old_open.scheduled_at) == old_open
        assert book.by_scheduled("a", T0) is None
        assert book.runs_for("a", DAY) == [old_open]
        assert book.get(new_done.run_id) == new_done
        assert book.published_runs(run_date=DAY, job="a") == [old_open]
        assert book.evict_final_before(date(2026, 10, 20)) == 0

    def test_open_for_lists_one_job(self) -> None:
        """List only the open runs of the asked-for job."""
        book = RunBook()
        mine = make_run()
        done = replace(make_run(seq=2, trigger=Trigger.MANUAL), state=RunState.SUCCEEDED)
        book.put(mine)
        book.put(done)
        assert book.open_for("a") == [mine]
        assert book.open_for("nobody") == []
        book.put(replace(mine, state=RunState.FAILED))
        assert book.open_for("a") == []

    def test_overlap_waiters_and_forget(self) -> None:
        """List every overlap waiter of a slot, and forget a run entirely."""
        book = RunBook()
        first = make_run(state=RunState.WAITING, reason=OVERLAP_REASON)
        second = make_run(
            seq=2, state=RunState.WAITING, reason=OVERLAP_REASON, trigger=Trigger.MANUAL
        )
        book.put(first)
        book.put(second)
        assert book.overlap_waiters("a", DAY) == [first, second]
        book.publish(first)
        book.forget(first.run_id)
        book.forget("nobody")
        assert book.get(first.run_id) is None
        assert book.overlap_waiters("a", DAY) == [second]
        assert book.published(first.run_id) == first

    def test_reopened_run_returns_to_indexes(self) -> None:
        """A final run reopened comes back to open, active and overlap views."""
        book = RunBook()
        done = replace(make_run(), state=RunState.FAILED)
        book.put(done)
        assert book.open_runs() == []
        sched = replace(done, state=RunState.SCHEDULED)
        book.put(sched)
        assert book.open_runs() == [sched]
        queued = replace(done, state=RunState.QUEUED)
        book.put(queued)
        assert book.active_for("a") == [queued]
        waiting = replace(done, state=RunState.WAITING, reason=OVERLAP_REASON)
        book.put(waiting)
        assert book.active_for("a") == []
        assert book.overlap_waiting("a", DAY) == waiting

    def test_open_and_active_sorted(self) -> None:
        """Open and active lists come back in (scheduled_at, run_id) order."""
        book = RunBook()
        later = make_run(seq=1, state=RunState.RUNNING, scheduled_at=T0 + timedelta(hours=1))
        early = make_run(seq=2, state=RunState.RUNNING)
        book.put(later)
        book.put(early)
        assert book.open_runs() == [early, later]
        assert book.active_for("a") == [early, later]

    def test_evict_published_without_current(self) -> None:
        """Final published copies with no current run are evicted too."""
        book = RunBook()
        orphan = replace(make_run(), state=RunState.SUCCEEDED)
        book.publish(orphan)
        assert book.evict_final_before(date(2026, 10, 20)) == 0
        assert book.published(orphan.run_id) is None
