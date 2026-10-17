"""Calendar inheritance: a dependency-only job borrows its upstreams' calendar.

A dependency-only job has no alarm clock of its own. It is like a younger
sibling who goes to school whenever the older ones do: it follows their
school days. So when it names no ``calendar:``, it takes the calendar of
the jobs it depends on (after they have taken theirs, all the way up). If
those upstreams disagree, there is no one calendar to take, and
validation reports it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pyscheduler.config.model import JobDef

__all__ = ["CalendarResolution", "resolve_calendars"]


@dataclass(frozen=True)
class CalendarResolution:
    """Each job's effective calendar, and the jobs whose upstreams disagree.

    Attributes:
        effective: Job name to calendar name (``None`` means no calendar).
        conflicts: For each dependency-only job whose upstreams disagree,
            each upstream's effective calendar. Those jobs get ``None``.

    """

    effective: Mapping[str, str | None]
    conflicts: Mapping[str, Mapping[str, str | None]]


def resolve_calendars(jobs: Mapping[str, JobDef]) -> CalendarResolution:
    """Work out every job's effective calendar.

    A job that names a calendar keeps it. A scheduled or manual job with
    none stays on no calendar. A dependency-only job with none inherits the
    one calendar its upstreams resolve to. Unknown upstreams are left out
    of the vote.

    Followers are decided in rounds, so a long chain needs no recursion.
    A follower is decided once all its upstreams are. When followers wait
    on each other in a loop (through run-date offsets), those that already
    have a decided upstream go ahead with just those votes; a loop with no
    way out gets no calendar.
    """
    effective: dict[str, str | None] = {}
    conflicts: dict[str, dict[str, str | None]] = {}
    waiting: dict[str, list[str]] = {}
    for job in jobs.values():
        if job.calendar is not None or not job.dependency_only:
            effective[job.name] = job.calendar
        else:
            waiting[job.name] = [d.job for d in job.depends_on if d.job in jobs]

    def decide(name: str, ups: list[str]) -> None:
        """Give ``name`` the one calendar its decided upstreams agree on."""
        votes = {up: effective[up] for up in ups if up in effective}
        names = set(votes.values())
        result: str | None = None
        if len(names) == 1:
            result = next(iter(names))
        elif len(names) > 1:
            conflicts[name] = votes
        effective[name] = result
        del waiting[name]

    while waiting:
        ready = [n for n, ups in waiting.items() if all(u in effective for u in ups)]
        if not ready:
            ready = [n for n, ups in waiting.items() if any(u in effective for u in ups)]
        if not ready:
            for name in list(waiting):
                decide(name, [])
            break
        for name in ready:
            decide(name, waiting[name])
    return CalendarResolution(effective, conflicts)
