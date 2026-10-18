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
from pyscheduler.config.model import Dependency, DepOn, JobDef
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
    if upstream is None or not upstream.is_final:
        state = upstream.state.value if upstream else "no run yet"
        return DepResult(
            Verdict.WAIT, f"waiting for {dep.job} ({upstream_date.isoformat()}): {state}"
        )
    label = f"{dep.job} ({upstream_date.isoformat()})"
    state = upstream.state
    if dep.on is DepOn.SUCCESS and state in _BAD_ON_SUCCESS:
        return DepResult(Verdict.BLOCK, f"blocked: {label} {state.value}")
    if dep.on is DepOn.FAILURE and state is not RunState.FAILED:
        return DepResult(Verdict.SKIP, f"skipped: {label} did not fail")
    return DepResult(Verdict.READY, "")
