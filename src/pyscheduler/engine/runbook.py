"""Keep every run in memory with indexes, like a school register with tabs.

The engine asks the same questions again and again: "what is the latest
run of ``extract`` for Monday?", "is anything of this job running?".
Looking through every run each time would get slower as the day fills
up, so the **run book** keeps small dictionaries ready, one per
question, and updates all of them whenever a run is put in.

The book also holds a second, separate copy of each run: the
**published** view. It only changes when the engine says the new state
has been safely written to the store, so the API never shows a state
that a crash could take back.
"""

import bisect
from collections.abc import Iterable
from datetime import date, datetime

from pyscheduler.engine.model import FINAL_STATES, Run, RunState, Trigger, parse_run_id

OVERLAP_REASON = "waiting for previous run"
"""The start of the reason on a run held back because its job is still running."""

_SCHEDULED_TRIGGERS = frozenset({Trigger.SCHEDULE, Trigger.CATCH_UP, Trigger.BACKFILL})
_ACTIVE_STATES = frozenset({RunState.QUEUED, RunState.RUNNING})

type _SortKey = tuple[datetime, str]
type _Slot = tuple[str, date]


class RunBook:
    """Hold all runs of the current process with O(1)-ish lookups."""

    def __init__(self) -> None:
        """Create an empty book."""
        self._runs: dict[str, Run] = {}
        self._sorted: dict[_Slot, list[_SortKey]] = {}
        self._by_scheduled: dict[tuple[str, datetime], str] = {}
        self._dependency: dict[_Slot, str] = {}
        self._active: dict[str, set[str]] = {}
        self._overlap: dict[_Slot, set[str]] = {}
        self._open: set[str] = set()
        self._open_by_job: dict[str, set[str]] = {}
        self._by_date: dict[date, set[str]] = {}
        self._max_seq: dict[_Slot, int] = {}
        self._published: dict[str, Run] = {}
        self._pub_by_date: dict[date, set[str]] = {}
        self._pub_by_job: dict[str, set[str]] = {}

    # -- current runs -------------------------------------------------

    def put(self, run: Run) -> None:
        """Add ``run``, or replace the run with the same id, updating every index.

        The engine must keep these unique, because the indexes hold one run
        each: ``(job, scheduled_at)`` for schedule, catch-up and backfill
        runs, and one dependency run per ``(job, run_date)``.
        """
        old = self._runs.get(run.run_id)
        if old is not None:
            self._unindex(old)
        self._runs[run.run_id] = run
        self._index(run)

    def get(self, run_id: str) -> Run | None:
        """Return the run with ``run_id``, or ``None``."""
        return self._runs.get(run_id)

    def runs_for(self, job: str, run_date: date) -> list[Run]:
        """Return the runs of ``job`` for ``run_date``, ordered by ``(scheduled_at, run_id)``."""
        return [self._runs[key[1]] for key in self._sorted.get((job, run_date), ())]

    def latest_for(self, job: str, run_date: date) -> Run | None:
        """Return the run with the greatest ``(scheduled_at, run_id)``, or ``None``."""
        keys = self._sorted.get((job, run_date))
        return self._runs[keys[-1][1]] if keys else None

    def by_scheduled(self, job: str, scheduled_at: datetime) -> Run | None:
        """Return the schedule, catch-up or backfill run of ``job`` due at ``scheduled_at``."""
        run_id = self._by_scheduled.get((job, scheduled_at))
        return self._runs[run_id] if run_id else None

    def dependency_run(self, job: str, run_date: date) -> Run | None:
        """Return the dependency-triggered run of ``job`` for ``run_date``."""
        run_id = self._dependency.get((job, run_date))
        return self._runs[run_id] if run_id else None

    def active_for(self, job: str) -> list[Run]:
        """Return the queued or running runs of ``job``."""
        return self._ordered(self._active.get(job, ()))

    def overlap_waiting(self, job: str, run_date: date) -> Run | None:
        """Return the run waiting for its job's previous run (the latest, if several)."""
        ids = self._overlap.get((job, run_date))
        if not ids:
            return None
        best = max(ids, key=lambda run_id: (self._runs[run_id].scheduled_at, run_id))
        return self._runs[best]

    def overlap_waiters(self, job: str, run_date: date) -> list[Run]:
        """Return every run of ``job`` for ``run_date`` waiting for its job's previous run."""
        return self._ordered(self._overlap.get((job, run_date), ()))

    def forget(self, run_id: str) -> None:
        """Drop ``run_id`` from the current runs (its published copy, if any, is kept)."""
        run = self._runs.pop(run_id, None)
        if run is not None:
            self._unindex(run)

    def next_seq(self, job: str, run_date: date) -> int:
        """Return the next unused sequence number for ``job`` on ``run_date``."""
        return self._max_seq.get((job, run_date), 0) + 1

    def seed_seq(self, job: str, run_date: date, seq: int) -> None:
        """Record that ``seq`` was already used (for example, found in the store)."""
        slot = (job, run_date)
        if seq > self._max_seq.get(slot, 0):
            self._max_seq[slot] = seq

    def open_runs(self) -> list[Run]:
        """Return every run that is not in a final state."""
        return self._ordered(self._open)

    def open_for(self, job: str) -> list[Run]:
        """Return the open (not final) runs of ``job``."""
        return self._ordered(self._open_by_job.get(job, ()))

    def evict_final_before(self, run_date: date) -> int:
        """Forget final runs dated before ``run_date``; return how many were removed."""
        doomed: list[Run] = []
        for day in [d for d in self._by_date if d < run_date]:
            doomed.extend(
                self._runs[run_id]
                for run_id in self._by_date[day]
                if self._runs[run_id].state in FINAL_STATES
            )
        for run in doomed:
            self._unindex(run)
            del self._runs[run.run_id]
            self._drop_published(run.run_id)
        for day in [d for d in self._pub_by_date if d < run_date]:
            for run_id in list(self._pub_by_date[day]):
                if self._published[run_id].state in FINAL_STATES and run_id not in self._runs:
                    self._drop_published(run_id)
        return len(doomed)

    # -- published views ----------------------------------------------

    def publish(self, run: Run) -> None:
        """Store ``run`` as the durable copy the API may show."""
        self._drop_published(run.run_id)
        self._published[run.run_id] = run
        self._pub_by_date.setdefault(run.run_date, set()).add(run.run_id)
        self._pub_by_job.setdefault(run.job, set()).add(run.run_id)

    def published(self, run_id: str) -> Run | None:
        """Return the published copy of ``run_id``, or ``None``."""
        return self._published.get(run_id)

    def published_runs(
        self,
        *,
        run_date: date | None = None,
        job: str | None = None,
        state: RunState | None = None,
    ) -> list[Run]:
        """Return published runs matching the filters, newest ``scheduled_at`` first."""
        ids: Iterable[str]
        if run_date is not None and job is not None:
            ids = self._pub_by_date.get(run_date, set()) & self._pub_by_job.get(job, set())
        elif run_date is not None:
            ids = self._pub_by_date.get(run_date, ())
        elif job is not None:
            ids = self._pub_by_job.get(job, ())
        else:
            ids = self._published
        found = [self._published[run_id] for run_id in ids]
        if state is not None:
            found = [run for run in found if run.state == state]
        found.sort(key=lambda run: run.run_id)
        found.sort(key=lambda run: run.scheduled_at, reverse=True)
        return found

    def _ordered(self, run_ids: Iterable[str]) -> list[Run]:
        """Return the runs for ``run_ids`` sorted by ``(scheduled_at, run_id)``."""
        runs = [self._runs[run_id] for run_id in run_ids]
        runs.sort(key=lambda run: (run.scheduled_at, run.run_id))
        return runs

    # -- index upkeep -------------------------------------------------

    def _index(self, run: Run) -> None:
        """Add ``run`` to every index."""
        slot = (run.job, run.run_date)
        bisect.insort(self._sorted.setdefault(slot, []), (run.scheduled_at, run.run_id))
        self._by_date.setdefault(run.run_date, set()).add(run.run_id)
        if run.trigger in _SCHEDULED_TRIGGERS:
            self._by_scheduled[(run.job, run.scheduled_at)] = run.run_id
        elif run.trigger is Trigger.DEPENDENCY:
            self._dependency[slot] = run.run_id
        if run.state in _ACTIVE_STATES:
            self._active.setdefault(run.job, set()).add(run.run_id)
        if run.state is RunState.WAITING and run.reason.startswith(OVERLAP_REASON):
            self._overlap.setdefault(slot, set()).add(run.run_id)
        if run.state not in FINAL_STATES:
            self._open.add(run.run_id)
            self._open_by_job.setdefault(run.job, set()).add(run.run_id)
        try:
            seq = parse_run_id(run.run_id)[2]
        except ValueError:
            return
        self.seed_seq(run.job, run.run_date, seq)

    def _unindex(self, run: Run) -> None:
        """Remove ``run`` from every index (the max sequence is kept on purpose)."""
        slot = (run.job, run.run_date)
        keys = self._sorted.get(slot, [])
        pos = bisect.bisect_left(keys, (run.scheduled_at, run.run_id))
        if pos < len(keys) and keys[pos] == (run.scheduled_at, run.run_id):
            del keys[pos]
        if not keys:
            self._sorted.pop(slot, None)
        _discard(self._by_date, run.run_date, run.run_id)
        if self._by_scheduled.get((run.job, run.scheduled_at)) == run.run_id:
            del self._by_scheduled[(run.job, run.scheduled_at)]
        if self._dependency.get(slot) == run.run_id:
            del self._dependency[slot]
        _discard(self._active, run.job, run.run_id)
        _discard(self._overlap, slot, run.run_id)
        self._open.discard(run.run_id)
        _discard(self._open_by_job, run.job, run.run_id)

    def _drop_published(self, run_id: str) -> None:
        """Remove the published copy of ``run_id``, if any, from the published indexes."""
        old = self._published.pop(run_id, None)
        if old is not None:
            _discard(self._pub_by_date, old.run_date, run_id)
            _discard(self._pub_by_job, old.job, run_id)


def _discard[K](index: dict[K, set[str]], key: K, run_id: str) -> None:
    """Remove ``run_id`` from the set at ``key``, dropping the set when it empties."""
    members = index.get(key)
    if members is None:
        return
    members.discard(run_id)
    if not members:
        del index[key]
