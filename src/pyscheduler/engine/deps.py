"""Decide whether a run may go yet: you can't ice the cake before it's baked.

Each entry in a job's ``depends_on`` names an upstream job, which
final state counts as "good enough" (``on``), and how many business
days back to look (``run_date``). Every entry gets a verdict and the
verdicts are combined: skip beats block beats wait beats ready.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pyscheduler.calendars import BusinessCalendar
from pyscheduler.config.model import Config, Dependency, DepOn, JobDef
from pyscheduler.engine.model import RunState
from pyscheduler.engine.runbook import RunBook

_OK_ON_SUCCESS = frozenset({RunState.SUCCEEDED, RunState.SKIPPED})
_BAD_ON_SUCCESS = frozenset({RunState.FAILED, RunState.CANCELLED})


class Verdict(StrEnum):
    """Name what a dependency check decided."""

    READY = "ready"
    WAIT = "wait"
    BLOCK = "block"
    SKIP = "skip"


@dataclass(frozen=True)
class DepResult:
    """Carry a verdict and the human reason for it."""

    verdict: Verdict
    reason: str


_PRECEDENCE = (Verdict.SKIP, Verdict.BLOCK, Verdict.WAIT)


def evaluate(job: JobDef, run_date: date, calendar: BusinessCalendar, book: RunBook) -> DepResult:
    """Return whether ``job`` may run for ``run_date`` given its upstream runs in ``book``."""
    results: list[DepResult] = []
    for dep in job.depends_on:
        try:
            upstream_date = calendar.shift(run_date, dep.run_date_offset)
        except ValueError:
            return DepResult(Verdict.WAIT, "waiting: calendar has no business days")
        results.append(_check(dep, upstream_date, book))
    for verdict in _PRECEDENCE:
        for result in results:
            if result.verdict is verdict:
                return result
    return DepResult(Verdict.READY, "")


def _check(dep: Dependency, upstream_date: date, book: RunBook) -> DepResult:
    """Judge one dependency against the latest upstream run for ``upstream_date``."""
    upstream = book.latest_for(dep.job, upstream_date)
    label = f"{dep.job} ({upstream_date.isoformat()})"
    if upstream is None:
        return DepResult(Verdict.WAIT, f"waiting for {label}: no run yet")
    if not upstream.is_final:
        # No upstream state in the text: it changes often (queued, running,
        # retry_wait) and every change would mean rewriting each waiting run.
        return DepResult(Verdict.WAIT, f"waiting for {label}")
    state = upstream.state
    if dep.on is DepOn.SUCCESS and state in _BAD_ON_SUCCESS:
        return DepResult(Verdict.BLOCK, f"blocked: {label} {state.value}")
    if dep.on is DepOn.FAILURE and state is not RunState.FAILED:
        return DepResult(Verdict.SKIP, f"skipped: {label} did not fail")
    return DepResult(Verdict.READY, "")


REACH_SLACK = 7
"""Calendar days added to a non-zero reach, for the weekends and holidays to come."""


def dependency_reach(config: Config, today: date) -> int:
    """Return how many calendar days back any dependency offset can look (ruling R27).

    For each dependency, the distance is ``today - calendar.shift(today,
    offset)`` on the downstream job's calendar; the largest one, plus
    ``REACH_SLACK`` days for weekends and holidays the measurement did not
    meet, is the reach. With no non-zero offset the reach is 0. The engine
    keeps final runs for ``memory_days`` plus this many days in memory,
    so a new run always finds the upstream runs it looks back at.
    """
    furthest = 0
    for job in config.jobs.values():
        calendar = config.calendar_for(job)
        for dep in job.depends_on:
            if dep.run_date_offset == 0:
                continue
            try:
                back = (today - calendar.shift(today, dep.run_date_offset)).days
            except ValueError, OverflowError:
                continue
            furthest = max(furthest, back)
    return furthest + REACH_SLACK if furthest else 0
