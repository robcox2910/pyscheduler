"""Semantic validation: the checks that need the whole config at once.

The loader is a clerk checking each box of a form. Validation is the
teacher who checks your work before the test: do the jobs you depend on
exist? Do two jobs wait for each other forever? Will this schedule ever
ring? Is a pool big enough? Every answer goes on one list, so you can fix
everything in one go.

Errors stop a load; warnings are kept on ``Config.warnings`` and the load
carries on.
"""

import difflib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar
from pyscheduler.config.calendar_inheritance import resolve_calendars
from pyscheduler.config.model import Config, Dependency, JobDef, Overlap, Problem, SourceRef
from pyscheduler.config.run_dates import (
    MAX_CHECKED_OFFSET,
    PAD_DAYS,
    WAIT_HORIZON_DAYS,
    RunDateIndex,
)
from pyscheduler.config.validate_alerts import check_alerts
from pyscheduler.config.validate_templates import check_placeholders
from pyscheduler.schedule.firings import next_firing
from pyscheduler.schedule.specs import BusinessDaySchedule

__all__ = ["find_cycles", "validate_config"]

MAX_BUSINESS_DAY = 31
"""The longest month has 31 days, so no calendar has more business days in one."""
EDGE_DAYS = 7
FIX_CALENDARS = "; put both jobs on one calendar"
UNKNOWN_SOURCE = SourceRef("<unknown>", 0)

type Severity = Literal["error", "warning"]


def _rotate(path: Sequence[str]) -> tuple[str, ...]:
    """Return a cycle's nodes rotated to start at the smallest name."""
    start = path.index(min(path))
    return (*path[start:], *path[:start])


def find_cycles(graph: Mapping[str, Sequence[str]]) -> list[tuple[str, ...]]:
    """Return the cycles found by a depth-first walk of ``graph``.

    Each cycle is written as a closed path that starts and ends at its
    smallest name, such as ``("a", "b", "a")``, and is listed once. One
    cycle is found per back edge of the walk, so every tangle of jobs that
    wait on each other gets at least one cycle reported, but not every
    elementary cycle through it is listed (that could be very many). Edges
    to names that are not keys of ``graph`` are ignored. The walk is
    iterative, so long chains cannot overflow the stack.
    """
    finished: set[str] = set()
    found: dict[tuple[str, ...], None] = {}
    for root in sorted(graph):
        if root in finished:
            continue
        path = [root]
        on_path = {root: 0}
        stack = [iter(sorted(set(graph[root])))]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                stack.pop()
                finished.add(path[-1])
                del on_path[path.pop()]
            elif child in on_path:
                cycle = _rotate(path[on_path[child] :])
                found[(*cycle, cycle[0])] = None
            elif child in graph and child not in finished:
                on_path[child] = len(path)
                path.append(child)
                stack.append(iter(sorted(set(graph[child]))))
    return list(found)


class _Checker:
    """Run every check over one config, collecting problems."""

    def __init__(self, config: Config, today: date, broken: frozenset[str]) -> None:
        """Prepare to check ``config``; ``broken`` jobs already have loader errors."""
        self.config = config
        self.today = today
        self.start = datetime.combine(today, time(0), tzinfo=UTC)
        self.broken = broken
        self.problems: list[Problem] = []
        self.dead_calendars: set[str] = set()
        resolution = resolve_calendars(config.jobs)
        self.effective = resolution.effective
        self.conflicts = resolution.conflicts
        self.index = RunDateIndex(config, today, self.calendar)

    def add(
        self, where: SourceRef, message: str, job: str | None, severity: Severity = "error"
    ) -> None:
        """Note one problem."""
        self.problems.append(Problem(where.file, where.line, message, job, severity))

    def calendar(self, job: JobDef) -> BusinessCalendar | None:
        """Return the job's usable effective calendar, or ``None`` if unknown or empty."""
        name = self.effective.get(job.name, job.calendar)
        if name is None:
            return ALL_DAYS
        if name in self.dead_calendars:
            return None
        return self.config.calendars.get(name)

    def run(self) -> list[Problem]:
        """Run every check and return the problems found."""
        self.check_calendars()
        self.check_cycles()
        check_alerts(self, self.config)
        for job in self.config.jobs.values():
            if job.name in self.broken:
                continue
            self.check_job(job)
            self.check_inherited_calendar(job)
            self.check_dependencies(job)
            self.check_wait_forever(job)
        return self.problems

    # --- calendars and cycles -------------------------------------------

    def check_calendars(self) -> None:
        """Check for calendars with no business days and for include cycles."""
        sources = self.config.calendar_sources
        for name, cal in self.config.calendars.items():
            if not cal.weekdays and not cal.extra_days:
                self.dead_calendars.add(name)
                where = sources.get(name, UNKNOWN_SOURCE)
                self.add(where, f"calendar {name!r} has no business days", None)
        for cycle in find_cycles(self.config.calendar_includes):
            where = sources.get(cycle[0], UNKNOWN_SOURCE)
            self.add(where, f"calendar include cycle: {' -> '.join(cycle)}", None)

    def check_cycles(self) -> None:
        """Report each same-run-date dependency cycle once."""
        graph = {
            name: [d.job for d in job.depends_on if d.run_date_offset == 0]
            for name, job in self.config.jobs.items()
        }
        for cycle in find_cycles(graph):
            job = self.config.jobs[cycle[0]]
            dep = next(d for d in job.depends_on if d.job == cycle[1] and d.run_date_offset == 0)
            self.add(dep.source, f"dependency cycle: {' -> '.join(cycle)}", job.name)

    # --- one job ---------------------------------------------------------

    def check_job(self, job: JobDef) -> None:
        """Check one job's own settings."""

        def error(message: str) -> None:
            """Note an error at the job's line."""
            self.add(job.source, message, job.name)

        if job.calendar is not None and job.calendar not in self.config.calendars:
            error(f"unknown calendar {job.calendar!r}")
        self.check_pools(job)
        check_placeholders(self, self.config, job)
        low, high, timeout = job.min_runtime, job.max_runtime, job.timeout
        if low is not None and high is not None and low >= high:
            error("min_runtime must be less than max_runtime")
        if high is not None and timeout is not None and high > timeout:
            error("max_runtime exceeds timeout")
        if job.retries.count < 0:
            error(f"negative retry count {job.retries.count}")
        if job.retries.backoff < 1:
            error(f"retries.backoff must be at least 1, got {job.retries.backoff}")
        if job.agent is not None:
            error(f"agent {job.agent!r} cannot be used yet: agents arrive in phase 4")
        if not job.schedules and not job.depends_on and not job.manual:
            error("no schedule, no dependencies and not manual: this job can never run")
        if job.overlap is Overlap.ALLOW and job.pools:
            self.add(
                job.source,
                "overlap allow with pools: overlapping runs may hog the pool",
                job.name,
                "warning",
            )
        self.check_schedules(job)

    def check_inherited_calendar(self, job: JobDef) -> None:
        """Check that a dependency-only job's upstreams agree on one calendar."""
        votes = self.conflicts.get(job.name)
        if votes is None or self.broken & votes.keys():
            return
        listed = ", ".join(f"{up}: {cal or 'none'}" for up, cal in sorted(votes.items()))
        message = (
            f"upstreams are on different calendars ({listed}): a dependency-only job "
            "inherits its upstreams' calendar, so they must share one"
        )
        self.add(job.source, message, job.name)

    def check_pools(self, job: JobDef) -> None:
        """Check that each pool exists and is big enough."""
        for pool, slots in job.pools.items():
            size = self.config.pools.get(pool)
            if size is None:
                self.add(job.source, f"unknown pool {pool!r}", job.name)
            elif slots > size:
                self.add(job.source, f"needs {slots} slots but pool {pool!r} has {size}", job.name)

    def check_schedules(self, job: JobDef) -> None:
        """Check business_day schedules and that the schedules fire at all."""
        in_range = True
        for schedule in job.schedules:
            if not isinstance(schedule, BusinessDaySchedule):
                continue
            if job.calendar is None:
                self.add(job.source, "business_day needs a calendar", job.name)
            if abs(schedule.n) > MAX_BUSINESS_DAY:
                in_range = False
                message = (
                    f"business_day must be between -{MAX_BUSINESS_DAY} and "
                    f"{MAX_BUSINESS_DAY} (not 0), got {schedule.n}"
                )
                self.add(job.source, message, job.name)
        cal = self.calendar(job)
        if not job.schedules or not in_range or cal is None:
            return
        try:
            first = next_firing(job, cal, self.start)
        except ValueError as exc:
            self.add(job.source, f"cannot work out the schedule: {exc}", job.name)
            return
        if first is None:
            self.add(job.source, "schedule never fires (checked over the next 4 years)", job.name)

    # --- dependencies ----------------------------------------------------

    def check_dependencies(self, job: JobDef) -> None:
        """Check each of the job's dependencies."""
        for dep in job.depends_on:
            if dep.run_date_offset > 0:
                message = (
                    f"positive run-date offset +{dep.run_date_offset} on {dep.job!r}: "
                    "use 0 (the same run date) or a negative number"
                )
                self.add(dep.source, message, job.name)
            elif dep.run_date_offset < -MAX_CHECKED_OFFSET:
                limit = f"-{MAX_CHECKED_OFFSET} and 0"
                self.add(dep.source, f"run_date offset must be between {limit}", job.name)
            up = self.config.jobs.get(dep.job)
            if up is None:
                if dep.job not in self.broken:
                    self.add(dep.source, self.unknown_job(dep.job), job.name)
                continue
            if dep.job in self.broken:
                continue
            if not up.enabled:
                self.add(dep.source, f"depends on disabled job {up.name!r}", job.name, "warning")
            self.check_dependency_calendars(job, dep, up)
            if up.enabled and self.index.repeats(up):
                message = (
                    f"depends on {up.name!r}, which fires more than once per run date "
                    "(only its latest run counts)"
                )
                self.add(dep.source, message, job.name, "warning")

    def unknown_job(self, name: str) -> str:
        """Return the message for an unknown upstream, with a "did you mean" hint."""
        close = difflib.get_close_matches(name, list(self.config.jobs), n=1)
        hint = f"; did you mean {close[0]!r}?" if close else ""
        return f"depends on unknown job {name!r}{hint}"

    def check_dependency_calendars(self, job: JobDef, dep: Dependency, up: JobDef) -> None:
        """Check that a job and its upstream share one effective calendar."""
        if job.name in self.conflicts or up.name in self.conflicts:
            return
        mine = self.effective.get(job.name, job.calendar)
        theirs = self.effective.get(up.name, up.calendar)
        known = self.config.calendars
        if mine == theirs or (mine and mine not in known) or (theirs and theirs not in known):
            return
        if mine and theirs:
            message = (
                f"different calendars: this job is on {mine!r} but {up.name!r} is on {theirs!r}"
            )
        elif mine:
            message = (
                f"calendar job depends on a job with no calendar: this job is on {mine!r} "
                f"but {up.name!r} has none"
            )
        else:
            message = (
                f"job with no calendar depends on a calendar job: {up.name!r} is on {theirs!r}"
            )
        self.add(dep.source, message + FIX_CALENDARS, job.name)

    # --- would wait forever ---------------------------------------------

    def wait_message(self, run_date: date, up: JobDef, target: date) -> str:
        """Describe a run date ``up`` will never have, naming the job at the root."""
        root, root_date = self.index.root_without(up, target)
        never = "is disabled" if not root.enabled else "has no firing for it"
        message = (
            f"would wait forever: its run for {run_date} needs a run of {up.name!r} for {target}"
        )
        if root is up:
            return message + f", which {never} and no way to get one"
        return message + (
            f", which only gets runs from upstream; {root.name!r} needs a run for "
            f"{root_date} but {never}"
        )

    def check_wait_forever(self, job: JobDef) -> None:
        """Warn when a scheduled job has run dates an upstream can never match."""
        cal = self.calendar(job)
        if not job.enabled or not job.schedules or cal is None:
            return
        mine = self.index.landings(job)
        if mine is None:
            return
        lowest = self.today - timedelta(days=PAD_DAYS - EDGE_DAYS)
        horizon = self.today + timedelta(days=WAIT_HORIZON_DAYS)
        for dep in job.depends_on:
            up = self.config.jobs.get(dep.job)
            if (
                up is None
                or dep.job in self.broken
                or not up.enabled
                or not -MAX_CHECKED_OFFSET <= dep.run_date_offset <= 0
            ):
                continue
            theirs = self.index.run_dates(up)
            if theirs is None:
                continue
            for landing, run_date in mine:
                if not self.today <= landing < horizon:
                    continue
                try:
                    target = cal.shift(run_date, dep.run_date_offset)
                except ValueError:
                    break
                if target >= lowest and target not in theirs:
                    message = self.wait_message(run_date, up, target)
                    self.add(dep.source, message, job.name, "warning")
                    break


def validate_config(
    config: Config, *, today: date | None = None, broken_jobs: frozenset[str] = frozenset()
) -> list[Problem]:
    """Check the whole config and return every error and warning, in file order.

    Args:
        config: The config to check.
        today: The day the "never fires" (4 years) and "would wait
            forever" (366 days) horizons start from; default today in UTC.
        broken_jobs: Jobs that already have loader errors. They are not
            checked again, and dependencies on them are not judged, so one
            mistake is not reported twice.

    Returns:
        The problems, sorted by file (in load order) and line.

    """
    day = today if today is not None else datetime.now(UTC).date()
    problems = _Checker(config, day, broken_jobs).run()
    order = {name: i for i, name in enumerate(config.sources)}
    return sorted(problems, key=lambda p: (order.get(p.file, -1), p.line))
