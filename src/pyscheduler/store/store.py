"""Keep runs, attempts, events and engine notes in a journalled PyDB database.

The store is the scheduler's notebook. Every change goes in through
``apply``, as one PyDB ``Transaction``: one journal entry and one
``fsync``, all or nothing. Reads come out as frozen model records.

Finding a row must not mean reading the whole table, so the store keeps
two little address books in memory: run id to PyDB record id, and
``(run id, attempt)`` to record id. They are built once when the store
opens and kept up to date by ``apply``, but only after a commit has
succeeded, so a rolled-back commit never leaves a wrong address behind.
Queries use PyDB indexes on the columns they filter by.

Every method is synchronous and holds PyDB's re-entrant lock, so the
store can be used from worker threads while another thread commits.
"""

from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Self, cast

from pydb.commits import is_committed
from pydb.database import Database
from pydb.record import Record, Value
from pydb.table import Table
from pydb.transaction import Transaction

from pyscheduler.engine.model import FINAL_STATES, Attempt, Event, Outcome, Run, RunState
from pyscheduler.store.schema import (
    ATTEMPTS,
    CONFIGS,
    ENGINE,
    EVENTS,
    INDEXES,
    RUNS,
    TABLES,
    AttemptKey,
    Batch,
    ConfigRecord,
    attempt_from_row,
    attempt_to_row,
    config_from_row,
    config_to_row,
    event_from_row,
    event_to_row,
    run_from_row,
    run_to_row,
)

OPEN_STATES = tuple(state for state in RunState if state not in FINAL_STATES)
"""The states a run can be in before it is final, in lifecycle order."""


class _Staged[K]:
    """Hold address-book changes made during a commit, until it succeeds."""

    def __init__(self, base: dict[K, int]) -> None:
        """Start with no changes on top of ``base``."""
        self.base = base
        self.changes: dict[K, int | None] = {}

    def get(self, key: K) -> int | None:
        """Return the record id of ``key``, seeing this commit's changes first."""
        if key in self.changes:
            return self.changes[key]
        return self.base.get(key)

    def put(self, key: K, record_id: int | None) -> None:
        """Note that ``key`` now lives at ``record_id`` (``None``: deleted)."""
        self.changes[key] = record_id

    def publish(self) -> None:
        """Copy the changes into the real address book."""
        for key, record_id in self.changes.items():
            if record_id is None:
                self.base.pop(key, None)
            else:
                self.base[key] = record_id


class Store:
    """Hide PyDB behind a small typed API for the engine.

    Every read takes PyDB's lock, so it can wait for a group commit that
    is in flight on the writer's thread (an ``fsync`` away). The engine
    must therefore call reads through ``run_in_thread``, never directly
    on the event loop's thread.
    """

    def __init__(self, db: Database) -> None:
        """Wrap an open database whose tables and indexes already exist."""
        self._db = db
        self._runs = db.get_table(RUNS)
        self._attempts = db.get_table(ATTEMPTS)
        self._events = db.get_table(EVENTS)
        self._engine = db.get_table(ENGINE)
        self._configs = db.get_table(CONFIGS)
        self._run_ids: dict[str, int] = {}
        self._attempt_ids: dict[AttemptKey, int] = {}
        self._engine_ids: dict[str, int] = {}
        self._config_ids: dict[int, int] = {}
        for record in self._runs.select():
            self._run_ids[cast(str, record["run_id"])] = record.record_id
        for record in self._attempts.select():
            key = (cast(str, record["run_id"]), cast(int, record["attempt"]))
            self._attempt_ids[key] = record.record_id
        for record in self._engine.select():
            self._engine_ids[cast(str, record["key"])] = record.record_id
        for record in self._configs.select():
            self._config_ids[cast(int, record["version"])] = record.record_id

    @classmethod
    def open(cls, state_dir: Path, *, sync: bool = True) -> Self:
        """Open (or create) the store in ``state_dir``, making missing tables and indexes."""
        state_dir.mkdir(parents=True, exist_ok=True)
        db = Database(state_dir / "db", journal=True, sync=sync)
        try:
            existing = set(db.table_names())
            for name, schema in TABLES.items():
                if name not in existing:
                    db.create_table(name, schema)
            for name, wanted in INDEXES.items():
                table = db.get_table(name)
                for index_name, column in wanted:
                    if index_name not in table.indexes:
                        table.create_index(index_name, column)
            return cls(db)
        except BaseException:
            db.close()
            raise

    def close(self) -> None:
        """Write a checkpoint, then close the database."""
        with self._db.lock:
            self._db.checkpoint()
            self._db.close()

    # Writing -----------------------------------------------------------

    def apply(self, batch: Batch) -> None:
        """Commit a batch as one transaction: deletes first, then upserts.

        Runs are upserted by run id, attempts by ``(run_id, attempt)``,
        engine notes by key and configs by version; events are appended.
        On failure nothing changes, in the database or in the lookups.
        """
        with self._db.lock:
            runs = _Staged(self._run_ids)
            attempts = _Staged(self._attempt_ids)
            engine = _Staged(self._engine_ids)
            configs = _Staged(self._config_ids)
            try:
                with Transaction(self._db):
                    for run_id in batch.deleted_run_ids:
                        self._delete_run(run_id, runs, attempts)
                    for run in batch.runs:
                        _upsert(self._runs, runs, run.run_id, run_to_row(run))
                    for attempt in batch.attempts:
                        key = (attempt.run_id, attempt.attempt)
                        _upsert(self._attempts, attempts, key, attempt_to_row(attempt))
                    for event in batch.events:
                        self._events.insert(event_to_row(event))
                    for key, value in batch.engine.items():
                        _upsert(self._engine, engine, key, {"key": key, "value": value})
                    for config in batch.configs:
                        _upsert(self._configs, configs, config.version, config_to_row(config))
            except BaseException as exc:
                if is_committed(exc):  # durable despite the error: keep the lookups true
                    _publish(runs, attempts, engine, configs)
                raise
            _publish(runs, attempts, engine, configs)

    def _delete_run(self, run_id: str, runs: _Staged[str], attempts: _Staged[AttemptKey]) -> None:
        """Delete a run with its attempts and events (inside the open transaction)."""
        for record in self._by_index(self._attempts, "attempts_run_id", run_id):
            self._attempts.delete(record.record_id)
            attempts.put((run_id, cast(int, record["attempt"])), None)
        for record in self._by_index(self._events, "events_run_id", run_id):
            self._events.delete(record.record_id)
        record_id = runs.get(run_id)
        if record_id is not None:
            self._runs.delete(record_id)
            runs.put(run_id, None)

    # Reading -----------------------------------------------------------

    def get_run(self, run_id: str) -> Run | None:
        """Return the run with this id, or ``None``."""
        with self._db.lock:
            record_id = self._run_ids.get(run_id)
            if record_id is None:
                return None
            return run_from_row(self._runs.get(record_id).data)

    def load_open_runs(self) -> list[Run]:
        """Return every run that is not in a final state."""
        with self._db.lock:
            return [
                run_from_row(record.data)
                for state in OPEN_STATES
                for record in self._by_index(self._runs, "runs_state", state.value)
            ]

    def load_final_runs_since(self, run_date: date) -> list[Run]:
        """Return the final runs whose run date is ``run_date`` or later."""
        with self._db.lock:
            runs = (run_from_row(r.data) for r in self._date_range(run_date, date.max))
            return [run for run in runs if run.is_final]

    def final_run_ids_before(self, run_date: date, limit: int) -> list[str]:
        """Return up to ``limit`` ids of final runs dated before ``run_date``, oldest first."""
        if run_date == date.min:
            return []
        with self._db.lock:
            records = self._date_range(date.min, run_date - timedelta(days=1))
            runs = [run_from_row(r.data) for r in records]
        final = sorted((r for r in runs if r.is_final), key=lambda r: (r.run_date, r.run_id))
        return [run.run_id for run in final[:limit]]

    def query_runs(
        self,
        *,
        run_date: date | None = None,
        job: str | None = None,
        state: RunState | None = None,
        limit: int = 500,
    ) -> list[Run]:
        """Return runs matching every given filter, newest ``scheduled_at`` first.

        Ties are broken by run id. The most selective index available
        (job, then run date, then state) picks the candidates.
        """
        with self._db.lock:
            if job is not None:
                records = self._by_index(self._runs, "runs_job", job)
            elif run_date is not None:
                records = self._by_index(self._runs, "runs_run_date", run_date)
            elif state is not None:
                records = self._by_index(self._runs, "runs_state", state.value)
            else:
                records = self._runs.select()
            runs = [run_from_row(r.data) for r in records]
        matching = [
            run
            for run in runs
            if (job is None or run.job == job)
            and (run_date is None or run.run_date == run_date)
            and (state is None or run.state == state)
        ]
        matching.sort(key=lambda r: r.run_id)
        matching.sort(key=lambda r: r.scheduled_at, reverse=True)
        return matching[:limit]

    def attempts_for(self, run_id: str) -> list[Attempt]:
        """Return a run's attempts, first attempt first."""
        with self._db.lock:
            records = self._by_index(self._attempts, "attempts_run_id", run_id)
            attempts = [attempt_from_row(r.data) for r in records]
        return sorted(attempts, key=lambda a: a.attempt)

    def events_for(self, run_id: str) -> list[Event]:
        """Return a run's events in the order they were written."""
        if not run_id:  # empty text is how scheduler-wide events are stored
            return []
        with self._db.lock:
            records = self._by_index(self._events, "events_run_id", run_id)
            return [event_from_row(r.data) for r in sorted(records, key=lambda r: r.record_id)]

    def recent_durations(self, job: str, limit: int = 10) -> list[timedelta]:
        """Return how long the job's latest succeeded attempts took, newest end first."""
        with self._db.lock:
            runs = self._by_index(self._runs, "runs_job", job)
            attempts = [
                attempt_from_row(record.data)
                for run in runs
                for record in self._by_index(self._attempts, "attempts_run_id", run["run_id"])
            ]
        done = [
            (a.ended_at, a.ended_at - a.started_at)
            for a in attempts
            if a.outcome is Outcome.SUCCEEDED and a.ended_at is not None
        ]
        done.sort(key=lambda pair: pair[0], reverse=True)
        return [took for _, took in done[:limit]]

    def get_engine(self, key: str) -> str | None:
        """Return the engine note stored under ``key``, or ``None``."""
        with self._db.lock:
            record_id = self._engine_ids.get(key)
            if record_id is None:
                return None
            return cast(str, self._engine.get(record_id)["value"])

    def latest_config(self) -> ConfigRecord | None:
        """Return the config record with the highest version, or ``None``."""
        with self._db.lock:
            if not self._config_ids:
                return None
            record_id = self._config_ids[max(self._config_ids)]
            return config_from_row(self._configs.get(record_id).data)

    # Helpers -----------------------------------------------------------

    @staticmethod
    def _by_index(table: Table, index_name: str, key: Value) -> list[Record]:
        """Return the table's records whose indexed column equals ``key``."""
        return table.select_by_index(table.indexes[index_name], key)

    def _date_range(self, low: date, high: date) -> Iterable[Record]:
        """Return runs records with a run date from ``low`` to ``high``, inclusive."""
        index = self._runs.indexes["runs_run_date"]
        return [self._runs.get(record_id) for record_id in index.find_range(low, high)]


def _upsert[K](table: Table, ids: _Staged[K], key: K, row: dict[str, Value]) -> None:
    """Update the row stored under ``key``, or insert it if there is none."""
    record_id = ids.get(key)
    if record_id is None:
        ids.put(key, table.insert(row).record_id)
    else:
        table.update(record_id, row)


def _publish(*staged: _Staged[str] | _Staged[AttemptKey] | _Staged[int]) -> None:
    """Copy every staged change into its address book."""
    for changes in staged:
        changes.publish()
