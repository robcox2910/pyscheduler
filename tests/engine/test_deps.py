"""Test dependency evaluation: can this run go yet."""

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar
from pyscheduler.config.model import (
    CatchUp,
    Criticality,
    Dependency,
    DepOn,
    JobAlerts,
    JobDef,
    Overlap,
    RetryPolicy,
    SourceRef,
)
from pyscheduler.engine.deps import DepResult, Verdict, evaluate
from pyscheduler.engine.model import Run, RunState, Trigger, make_run_id
from pyscheduler.engine.runbook import RunBook
from pyscheduler.schedule.firings import RunDateRule

MONDAY = date(2026, 10, 19)
FRIDAY = date(2026, 10, 16)
SUNDAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
MON_FRI = frozenset(range(5))
NO_DATES: frozenset[date] = frozenset()
UK = BusinessCalendar("uk", MON_FRI, NO_DATES, NO_DATES)
EMPTY = BusinessCalendar("empty", frozenset(), NO_DATES, NO_DATES)


def make_job(*deps: Dependency) -> JobDef:
    """Build a job that depends on the given dependencies."""
    return JobDef(
        name="down",
        command="true",
        cwd=None,
        env={},
        schedules=(),
        manual=False,
        calendar=None,
        timezone=ZoneInfo("UTC"),
        run_date_rule=RunDateRule.SAME_DAY,
        depends_on=deps,
        retries=RetryPolicy(),
        timeout=None,
        criticality=Criticality.NORMAL,
        pools={},
        overlap=Overlap.QUEUE,
        catch_up=CatchUp.LATEST,
        stdout="o",
        stderr="e",
        min_runtime=None,
        max_runtime=None,
        must_start_by=None,
        alerts=JobAlerts(),
        agent=None,
        agent_tags=(),
        tags=(),
        description="",
        owner="",
        enabled=True,
        source=SourceRef("t", 1),
    )


def add(book: RunBook, job: str, run_date: date, state: RunState, seq: int = 1) -> None:
    """Put a run of ``job`` in the given state into the book."""
    book.put(
        Run(
            run_id=make_run_id(job, run_date, seq),
            job=job,
            run_date=run_date,
            scheduled_at=NOW,
            state=state,
            reason="",
            attempt=0,
            rerun=0,
            trigger=Trigger.SCHEDULE,
            config_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def check(
    deps: tuple[Dependency, ...], book: RunBook, calendar: BusinessCalendar = ALL_DAYS
) -> DepResult:
    """Evaluate a job with ``deps`` for Monday."""
    return evaluate(make_job(*deps), MONDAY, calendar, book)


class TestEvaluate:
    """Each rule of the dependency table."""

    def test_no_dependencies_ready(self) -> None:
        """A job with nothing to wait for is ready."""
        assert check((), RunBook()) == DepResult(Verdict.READY, "")

    @pytest.mark.parametrize(
        "state", [None, RunState.RUNNING, RunState.WAITING, RunState.RETRY_WAIT]
    )
    @pytest.mark.parametrize("on", list(DepOn))
    def test_not_final_waits(self, state: RunState | None, on: DepOn) -> None:
        """No run, or a run that is not final, means wait."""
        book = RunBook()
        if state is not None:
            add(book, "up", MONDAY, state)
        result = check((Dependency("up", on),), book)
        shown = "waiting for up (2026-10-19)" + ("" if state else ": no run yet")
        assert result == DepResult(Verdict.WAIT, shown)

    @pytest.mark.parametrize("state", [RunState.SUCCEEDED, RunState.SKIPPED])
    def test_success_ok(self, state: RunState) -> None:
        """On success, succeeded and skipped are fine."""
        book = RunBook()
        add(book, "up", MONDAY, state)
        assert check((Dependency("up"),), book) == DepResult(Verdict.READY, "")

    @pytest.mark.parametrize("state", [RunState.FAILED, RunState.CANCELLED])
    def test_success_blocks(self, state: RunState) -> None:
        """On success, failed and cancelled block."""
        book = RunBook()
        add(book, "up", MONDAY, state)
        assert check((Dependency("up"),), book) == DepResult(
            Verdict.BLOCK, f"blocked: up (2026-10-19) {state.value}"
        )

    def test_failure_ok(self) -> None:
        """On failure, a failed upstream is fine."""
        book = RunBook()
        add(book, "up", MONDAY, RunState.FAILED)
        assert check((Dependency("up", DepOn.FAILURE),), book).verdict is Verdict.READY

    @pytest.mark.parametrize("state", [RunState.SUCCEEDED, RunState.SKIPPED, RunState.CANCELLED])
    def test_failure_skips(self, state: RunState) -> None:
        """On failure, anything else final skips."""
        book = RunBook()
        add(book, "up", MONDAY, state)
        assert check((Dependency("up", DepOn.FAILURE),), book) == DepResult(
            Verdict.SKIP, "skipped: up (2026-10-19) did not fail"
        )

    @pytest.mark.parametrize(
        "state",
        [RunState.SUCCEEDED, RunState.FAILED, RunState.SKIPPED, RunState.CANCELLED],
    )
    def test_done_any_final(self, state: RunState) -> None:
        """On done, every final state is fine."""
        book = RunBook()
        add(book, "up", MONDAY, state)
        assert check((Dependency("up", DepOn.DONE),), book).verdict is Verdict.READY

    def test_offset_over_weekend(self) -> None:
        """A run_date of -1 on Monday looks at Friday, not Sunday."""
        book = RunBook()
        add(book, "up", FRIDAY, RunState.SUCCEEDED)
        add(book, "up", SUNDAY, RunState.FAILED)
        dep = Dependency("up", run_date_offset=-1)
        assert check((dep,), book, UK).verdict is Verdict.READY
        assert check((dep,), book, ALL_DAYS) == DepResult(
            Verdict.BLOCK, "blocked: up (2026-10-18) failed"
        )

    def test_latest_run_decides(self) -> None:
        """A rerun (later run) replaces an earlier failure."""
        book = RunBook()
        add(book, "up", MONDAY, RunState.FAILED, seq=1)
        book.put(
            replace(
                book.runs_for("up", MONDAY)[0],
                run_id=make_run_id("up", MONDAY, 2),
                scheduled_at=datetime(2026, 10, 19, 10, tzinfo=UTC),
                state=RunState.SUCCEEDED,
            )
        )
        assert check((Dependency("up"),), book).verdict is Verdict.READY

    def test_no_business_days(self) -> None:
        """A calendar with no business days makes the job wait."""
        result = check((Dependency("up", run_date_offset=-1),), RunBook(), EMPTY)
        assert result == DepResult(Verdict.WAIT, "waiting: calendar has no business days")

    def test_precedence(self) -> None:
        """Skip beats block beats wait beats ready; first dependency wins ties."""
        book = RunBook()
        add(book, "ok", MONDAY, RunState.SUCCEEDED)
        add(book, "bad1", MONDAY, RunState.FAILED)
        add(book, "bad2", MONDAY, RunState.CANCELLED)
        add(book, "fine", MONDAY, RunState.SUCCEEDED)
        add(book, "slow", MONDAY, RunState.RUNNING)
        ready = Dependency("ok")
        wait = Dependency("slow")
        block1 = Dependency("bad1")
        block2 = Dependency("bad2")
        skip = Dependency("fine", DepOn.FAILURE)
        assert check((ready, wait), book).verdict is Verdict.WAIT
        assert check((wait, block1), book) == DepResult(
            Verdict.BLOCK, "blocked: bad1 (2026-10-19) failed"
        )
        assert check((block1, block2), book).reason == "blocked: bad1 (2026-10-19) failed"
        assert check((block2, block1), book).reason == "blocked: bad2 (2026-10-19) cancelled"
        assert check((wait, block1, skip), book) == DepResult(
            Verdict.SKIP, "skipped: fine (2026-10-19) did not fail"
        )
