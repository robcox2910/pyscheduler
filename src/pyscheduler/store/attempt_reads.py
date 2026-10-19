"""Fetch the attempts of many runs at once.

Think of a teacher collecting the homework books of a whole class in one
trip to the cupboard instead of one trip per pupil. ``attempts_for_runs``
opens the cupboard (takes the database lock) once and brings back every
asked-for run's attempts, which is how the timeline fills a day of lanes.
"""

from collections.abc import Collection

from pydb.database import Database
from pydb.table import Table

from pyscheduler.engine.model import Attempt
from pyscheduler.store.schema import attempt_from_row

__all__ = ["AttemptReads"]


class AttemptReads:
    """Give the ``Store`` its batched attempts read, under one hold of PyDB's lock."""

    _db: Database
    _attempts: Table

    def attempts_for_runs(self, run_ids: Collection[str]) -> dict[str, list[Attempt]]:
        """Return each run's attempts, first attempt first (``[]`` for a run with none)."""
        found: dict[str, list[Attempt]] = {}
        with self._db.lock:
            index = self._attempts.indexes["attempts_run_id"]
            for run_id in run_ids:
                records = self._attempts.select_by_index(index, run_id)
                found[run_id] = [attempt_from_row(record.data) for record in records]
        for attempts in found.values():
            attempts.sort(key=lambda a: a.attempt)
        return found
