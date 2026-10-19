"""How long jobs usually take: their latest succeeded attempts' run times.

Think of a stopwatch notebook kept for every runner. Asking about one
runner means flipping to their page; a busy board asks about a hundred
runners at once, so ``recent_durations_for`` flips through all their
pages in one sitting (one hold of the database lock) instead of
queueing up a hundred times.
"""

from collections.abc import Collection
from datetime import timedelta

from pydb.database import Database
from pydb.table import Table

from pyscheduler.engine.model import Attempt, Outcome
from pyscheduler.store.schema import attempt_from_row

__all__ = ["DurationReads"]

DEFAULT_SAMPLE = 10
"""How many recent durations a job's read returns unless told otherwise."""


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
        """Return ``recent_durations`` for every job in ``jobs`` (``[]`` if none), in one pass."""
        found: dict[str, list[Attempt]] = {}
        with self._db.lock:
            runs_index = self._runs.indexes["runs_job"]
            attempts_index = self._attempts.indexes["attempts_run_id"]
            for job in jobs:
                found[job] = [
                    attempt_from_row(record.data)
                    for run in self._runs.select_by_index(runs_index, job)
                    for record in self._attempts.select_by_index(attempts_index, run["run_id"])
                ]
        return {job: _newest(attempts, limit) for job, attempts in found.items()}


def _newest(attempts: list[Attempt], limit: int) -> list[timedelta]:
    """Return the durations of the newest ``limit`` succeeded attempts, newest end first."""
    done = [
        (a.ended_at, a.ended_at - a.started_at)
        for a in attempts
        if a.outcome is Outcome.SUCCEEDED and a.ended_at is not None
    ]
    done.sort(key=lambda pair: pair[0], reverse=True)
    return [took for _, took in done[:limit]]
