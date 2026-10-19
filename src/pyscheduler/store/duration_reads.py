"""How long jobs usually take: their latest succeeded attempts' run times.

Think of a stopwatch notebook kept for every runner. Asking about one
runner means flipping to their page; a busy board asks about a hundred
runners at once, so ``recent_durations_for`` flips through all their
pages in one sitting (one hold of the database lock) instead of
queueing up a hundred times.
"""

from collections.abc import Collection, Mapping
from datetime import date, datetime, timedelta
from typing import cast

from pydb.database import Database
from pydb.record import Value
from pydb.table import Table

from pyscheduler.engine.model import Attempt, Outcome
from pyscheduler.store.schema import attempt_from_row

__all__ = ["DurationReads"]

DEFAULT_SAMPLE = 10
"""How many recent durations a job's read returns unless told otherwise."""

SCAN_RUNS = 100
"""The most runs of one job a read looks through, newest run date first.

This keeps the cost per job fixed however long its history grows. A job
needs its ten newest succeeded runs among its newest ``SCAN_RUNS`` runs
(normally days or weeks of them); one that has failed for longer than
that simply has no recent durations.
"""


class DurationReads:
    """Give the ``Store`` its duration reads, each taking PyDB's lock (see ``Store``)."""

    _db: Database
    _runs: Table
    _attempts: Table

    def recent_durations(self, job: str, limit: int = DEFAULT_SAMPLE) -> list[timedelta]:
        """Return how long the job's latest succeeded attempts took, newest end first."""
        return self.recent_durations_for([job], limit)[job]

    def recent_durations_for(
        self, jobs: Collection[str], limit: int = DEFAULT_SAMPLE
    ) -> dict[str, list[timedelta]]:
        """Return ``recent_durations`` for every job in ``jobs`` (``[]`` if none), in one pass.

        Only each job's newest ``SCAN_RUNS`` runs (by run date, then due time) are read.
        """
        found: dict[str, list[Attempt]] = {}
        with self._db.lock:
            runs_index = self._runs.indexes["runs_job"]
            attempts_index = self._attempts.indexes["attempts_run_id"]
            for job in jobs:
                rows = [record.data for record in self._runs.select_by_index(runs_index, job)]
                rows.sort(key=_newest_run_first, reverse=True)
                found[job] = [
                    attempt_from_row(record.data)
                    for run in rows[:SCAN_RUNS]
                    for record in self._attempts.select_by_index(attempts_index, run["run_id"])
                ]
        return {job: _newest(attempts, limit) for job, attempts in found.items()}


def _newest_run_first(row: Mapping[str, Value]) -> tuple[date, datetime]:
    """Return the sort key of a run row: its run date, then its due time."""
    return (cast(date, row["run_date"]), cast(datetime, row["scheduled_at"]))


def _newest(attempts: list[Attempt], limit: int) -> list[timedelta]:
    """Return the durations of the newest ``limit`` succeeded attempts, newest end first."""
    done = [
        (a.ended_at, a.ended_at - a.started_at)
        for a in attempts
        if a.outcome is Outcome.SUCCEEDED and a.ended_at is not None
    ]
    done.sort(key=lambda pair: pair[0], reverse=True)
    return [took for _, took in done[:limit]]
