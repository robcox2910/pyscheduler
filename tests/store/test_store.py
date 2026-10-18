"""Tests for the store: PyDB tables behind a small typed API."""

import dataclasses
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydb.database import Database
from pydb.errors import PyDBError
from pydb.schema import Column, Schema
from pydb.types import DataType
from store_helpers import DAY, T0, make_attempt, make_run

from pyscheduler.engine.model import Event, Outcome, RunState
from pyscheduler.store import Batch, ConfigRecord, Store, StoreSchemaError
from pyscheduler.store.schema import TABLES
from pyscheduler.store.store import SCHEMA_VERSION, SCHEMA_VERSION_KEY

NEXT_DAY = DAY + timedelta(days=1)
PREV_DAY = DAY - timedelta(days=1)
MINUTE = timedelta(minutes=1)
LIMIT_TWO = 2
EXIT_CODE = 0
PID = 4321
CONFIG_VERSION = 7
THREE = 3


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Open a fresh store without fsync (tests do not need power-cut safety)."""
    return Store.open(tmp_path / "state", sync=False)


def reopen(store: Store, tmp_path: Path) -> Store:
    """Close ``store`` and open the same folder again."""
    store.close()
    return Store.open(tmp_path / "state", sync=False)


class TestBatch:
    """Batches merge in order, later upserts winning."""

    def test_empty(self) -> None:
        """Report an empty batch as empty."""
        assert Batch().is_empty
        assert not Batch(engine={"k": "v"}).is_empty

    def test_merge_later_upsert_wins(self) -> None:
        """Keep one copy of a run, attempt or engine key: the later one."""
        first = make_run()
        second = dataclasses.replace(first, state=RunState.QUEUED)
        merged = Batch(runs=[first], attempts=[make_attempt(first.run_id)], engine={"k": "1"})
        late_attempt = make_attempt(first.run_id, took=MINUTE)
        merged.merge(Batch(runs=[second], attempts=[late_attempt], engine={"k": "2"}))
        assert merged.runs == [second]
        assert merged.attempts == [late_attempt]
        assert merged.engine == {"k": "2"}

    def test_merge_later_delete_drops_earlier_upserts(self) -> None:
        """Drop pending upserts of a run that a later batch deletes."""
        run = make_run()
        other = make_run(seq=2)
        event = Event(T0, "engine", run.run_id, "created", "{}")
        merged = Batch(runs=[run, other], attempts=[make_attempt(run.run_id)], events=[event])
        merged.merge(Batch(deleted_run_ids=[run.run_id]))
        assert merged.runs == [other]
        assert merged.attempts == []
        assert merged.events == []
        assert merged.deleted_run_ids == [run.run_id]

    def test_merge_upsert_after_delete_is_kept(self) -> None:
        """Keep a run upserted after its delete (deletes apply first)."""
        run = make_run()
        merged = Batch(deleted_run_ids=[run.run_id])
        merged.merge(Batch(runs=[run]))
        assert merged.runs == [run]
        assert merged.deleted_run_ids == [run.run_id]


class TestStore:
    """Upserts, reloads, queries and cascading deletes."""

    def test_apply_upsert_and_reload(self, store: Store, tmp_path: Path) -> None:
        """Keep the second state of a run upserted twice, across a reopen."""
        run = make_run(held_from=RunState.QUEUED)
        store.apply(Batch(runs=[run]))
        later = dataclasses.replace(run, state=RunState.RUNNING, attempt=1, held_from=None)
        store.apply(Batch(runs=[later]))
        store = reopen(store, tmp_path)
        loaded = store.get_run(run.run_id)
        assert loaded == later
        assert loaded is not None
        assert loaded.scheduled_at.tzinfo is UTC
        assert type(loaded.run_date) is date
        held = dataclasses.replace(later, state=RunState.HELD, held_from=RunState.RUNNING)
        store.apply(Batch(runs=[held]))
        assert store.get_run(run.run_id) == held
        store.close()

    def test_get_missing_run(self, store: Store) -> None:
        """Return ``None`` for an unknown run id."""
        assert store.get_run("nope.2026-10-17.1") is None
        store.close()

    def test_open_runs_only_non_final(self, store: Store) -> None:
        """Load every run that is not in a final state."""
        live = [make_run(seq=1, state=RunState.WAITING), make_run(seq=2, state=RunState.RUNNING)]
        done = [make_run(seq=3, state=RunState.SUCCEEDED), make_run(seq=4, state=RunState.FAILED)]
        store.apply(Batch(runs=live + done))
        assert sorted(r.run_id for r in store.load_open_runs()) == sorted(r.run_id for r in live)
        store.close()

    def test_attempts_and_events_roundtrip(self, store: Store, tmp_path: Path) -> None:
        """Round-trip attempts (with and without an end) and events in order."""
        run = make_run(state=RunState.RUNNING)
        running = make_attempt(run.run_id, 2, started_at=T0 + MINUTE)
        ended = make_attempt(run.run_id, 1, took=MINUTE, outcome=Outcome.FAILED)
        events = [
            Event(T0, "engine", run.run_id, "created", '{"a": 1}'),
            Event(T0 + MINUTE, "alice", run.run_id, "hold", "{}"),
            Event(T0, "engine", None, "config_loaded", "{}"),
        ]
        store.apply(Batch(runs=[run], attempts=[running, ended], events=events))
        store = reopen(store, tmp_path)
        assert store.attempts_for(run.run_id) == [ended, running]
        assert ended.exit_code == EXIT_CODE
        assert ended.pid == PID
        assert store.events_for(run.run_id) == events[:2]
        assert store.events_for("") == []
        store.close()

    def test_latest_attempts(self, store: Store) -> None:
        """Return each run's newest attempt, leaving out runs with none."""
        run, other = make_run(seq=1), make_run(seq=2)
        first = make_attempt(run.run_id, 1, took=MINUTE, outcome=Outcome.FAILED)
        second = make_attempt(run.run_id, 2, started_at=T0 + MINUTE)
        store.apply(Batch(runs=[run, other], attempts=[second, first]))
        assert store.latest_attempts([run.run_id, other.run_id]) == {run.run_id: second}
        store.close()

    def test_attempt_upsert(self, store: Store) -> None:
        """Replace an attempt with the same run id and number."""
        run = make_run()
        store.apply(Batch(runs=[run], attempts=[make_attempt(run.run_id)]))
        done = make_attempt(run.run_id, took=MINUTE, outcome=Outcome.SUCCEEDED)
        store.apply(Batch(attempts=[done]))
        assert store.attempts_for(run.run_id) == [done]
        store.close()

    def test_delete_cascades(self, store: Store, tmp_path: Path) -> None:
        """Delete a run together with its attempts and events, and nothing else."""
        gone, kept = make_run(seq=1), make_run(seq=2)
        store.apply(
            Batch(
                runs=[gone, kept],
                attempts=[make_attempt(gone.run_id), make_attempt(kept.run_id)],
                events=[
                    Event(T0, "engine", gone.run_id, "created", "{}"),
                    Event(T0, "engine", kept.run_id, "created", "{}"),
                ],
            )
        )
        store.apply(Batch(deleted_run_ids=[gone.run_id, "never.2026-10-17.1"]))
        assert store.get_run(gone.run_id) is None
        assert store.attempts_for(gone.run_id) == []
        assert store.events_for(gone.run_id) == []
        store = reopen(store, tmp_path)
        assert store.get_run(gone.run_id) is None
        assert store.attempts_for(kept.run_id) != []
        assert store.events_for(kept.run_id) != []
        store.apply(Batch(runs=[gone]))
        assert store.get_run(gone.run_id) == gone
        store.close()

    def test_recent_durations_newest_first(self, store: Store) -> None:
        """Return succeeded attempts' durations, newest end first, up to the limit."""
        runs = [make_run(seq=n) for n in (1, 2, 3, 4)]
        attempts = [
            make_attempt(runs[0].run_id, took=1 * MINUTE, outcome=Outcome.SUCCEEDED),
            make_attempt(
                runs[1].run_id,
                started_at=T0 + 10 * MINUTE,
                took=2 * MINUTE,
                outcome=Outcome.SUCCEEDED,
            ),
            make_attempt(
                runs[2].run_id, started_at=T0 + 20 * MINUTE, took=3 * MINUTE, outcome=Outcome.FAILED
            ),
            make_attempt(
                runs[3].run_id,
                started_at=T0 + 30 * MINUTE,
                took=4 * MINUTE,
                outcome=Outcome.SUCCEEDED,
            ),
        ]
        other = make_run(job="other")
        other_attempt = make_attempt(other.run_id, took=9 * MINUTE, outcome=Outcome.SUCCEEDED)
        store.apply(Batch(runs=[*runs, other], attempts=[*attempts, other_attempt]))
        assert store.recent_durations("extract") == [4 * MINUTE, 2 * MINUTE, 1 * MINUTE]
        assert store.recent_durations("extract", limit=LIMIT_TWO) == [4 * MINUTE, 2 * MINUTE]
        assert store.recent_durations("missing") == []
        store.close()

    def test_query_runs(self, store: Store) -> None:
        """Filter by job, run date and state, newest scheduled first, then by id."""
        a1 = make_run("a", DAY, 1, scheduled_at=T0)
        a2 = make_run("a", DAY, 2, RunState.SUCCEEDED, scheduled_at=T0 + MINUTE)
        a3 = make_run("a", NEXT_DAY, 1, scheduled_at=T0 + MINUTE)
        b1 = make_run("b", DAY, 1, RunState.SUCCEEDED, scheduled_at=T0)
        store.apply(Batch(runs=[a1, a2, a3, b1]))
        assert store.query_runs() == [a2, a3, a1, b1]
        assert store.query_runs(job="a") == [a2, a3, a1]
        assert store.query_runs(run_date=DAY) == [a2, a1, b1]
        assert store.query_runs(state=RunState.SUCCEEDED) == [a2, b1]
        assert store.query_runs(job="a", run_date=DAY, state=RunState.SCHEDULED) == [a1]
        assert store.query_runs(job="a", limit=LIMIT_TWO) == [a2, a3]
        store.close()

    def test_final_runs_by_date(self, store: Store) -> None:
        """Load final runs since a date, and list ids before one, oldest first."""
        old = make_run("a", PREV_DAY, 1, RunState.SUCCEEDED)
        old_b = make_run("b", PREV_DAY, 1, RunState.CANCELLED)
        older_open = make_run("a", PREV_DAY, 2, RunState.WAITING)
        today = make_run("a", DAY, 1, RunState.FAILED)
        today_open = make_run("a", DAY, 2, RunState.RUNNING)
        store.apply(Batch(runs=[today, old_b, old, older_open, today_open]))
        assert store.load_final_runs_since(DAY) == [today]
        since = store.load_final_runs_since(PREV_DAY)
        assert sorted(r.run_id for r in since) == sorted([old.run_id, old_b.run_id, today.run_id])
        assert store.final_run_ids_before(NEXT_DAY, limit=LIMIT_TWO) == [old.run_id, old_b.run_id]
        assert store.final_run_ids_before(DAY, limit=10) == [old.run_id, old_b.run_id]
        assert store.final_run_ids_before(date.min, limit=10) == []
        store.close()

    def test_engine_keys_and_configs(self, store: Store, tmp_path: Path) -> None:
        """Upsert engine keys and keep the highest config version as the latest."""
        assert store.get_engine("last_tick") is None
        assert store.latest_config() is None
        first = ConfigRecord(1, T0, "abc", '{"a.yaml": "x"}')
        latest = ConfigRecord(CONFIG_VERSION, T0 + MINUTE, "def", "{}")
        store.apply(Batch(engine={"last_tick": "one"}, configs=[latest, first]))
        store.apply(Batch(engine={"last_tick": "two"}))
        store = reopen(store, tmp_path)
        assert store.get_engine("last_tick") == "two"
        assert store.latest_config() == latest
        store.close()

    def test_failed_apply_rolls_back_and_keeps_lookups(self, store: Store) -> None:
        """Roll back a failing batch entirely; a retry of the good part then works."""
        good = make_run(seq=1)
        bad_time = datetime(2026, 10, 17, 9)  # naive: PyDB refuses it  # noqa: DTZ001
        bad = dataclasses.replace(make_run(seq=2), scheduled_at=bad_time)
        with pytest.raises(PyDBError):
            store.apply(Batch(runs=[good, bad]))
        assert store.get_run(good.run_id) is None
        assert store.query_runs() == []
        store.apply(Batch(runs=[good]))
        assert store.get_run(good.run_id) == good
        store.close()

    def test_reopen_is_idempotent(self, tmp_path: Path) -> None:
        """Open the same folder several times without recreating anything."""
        Store.open(tmp_path / "state", sync=False).close()
        store = Store.open(tmp_path / "state", sync=False)
        store.apply(Batch(runs=[make_run()]))
        store = reopen(store, tmp_path)
        assert len(store.query_runs()) == 1
        store.close()


class TestNullsAndSchemaVersion:
    """Missing values are real NULLs, and old layouts are refused."""

    def test_missing_values_are_null(self, store: Store) -> None:
        """Store ``None`` as NULL in native columns, and skip NULL run ids."""
        run = make_run()
        running = make_attempt(run.run_id)
        wide = Event(T0, "engine", None, "config_loaded", "{}")
        store.apply(Batch(runs=[run], attempts=[running], events=[wide]))
        db = store._db
        attempt_row = db.get_table("attempts").select()[0]
        assert attempt_row["ended_at"] is None
        assert attempt_row["exit_code"] is None
        assert attempt_row["outcome"] is None
        assert db.get_table("runs").select()[0]["held_from"] is None
        assert db.get_table("events").select()[0]["run_id"] is None
        assert store.events_for(run.run_id) == []
        assert store.attempts_for(run.run_id) == [running]
        store.close()

    def test_schema_version_recorded(self, store: Store) -> None:
        """Write the schema version on first open."""
        assert store.get_engine(SCHEMA_VERSION_KEY) == SCHEMA_VERSION
        store.close()

    def test_old_layout_refused(self, tmp_path: Path) -> None:
        """Refuse a state folder whose tables have no schema version."""
        db = Database(tmp_path / "state" / "db", journal=True, sync=False)
        db.create_table("runs", Schema([Column("run_id", DataType.TEXT, primary_key=True)]))
        db.get_table("runs").insert({"run_id": "a.2026-10-17.1"})
        db.close()
        with pytest.raises(StoreSchemaError, match="older pyscheduler schema; delete it"):
            Store.open(tmp_path / "state", sync=False)
        # The failed open let go of the folder, so it can be opened again.
        Database(tmp_path / "state" / "db", journal=True, sync=False).close()

    def test_wrong_version_refused(self, store: Store, tmp_path: Path) -> None:
        """Refuse a store stamped with another schema version."""
        store.apply(Batch(engine={SCHEMA_VERSION_KEY: "1"}))
        store.close()
        with pytest.raises(StoreSchemaError, match="found schema '1'"):
            Store.open(tmp_path / "state", sync=False)

    def test_half_made_store_is_adopted(self, tmp_path: Path) -> None:
        """Adopt a folder whose first open stopped after making the empty engine table."""
        db = Database(tmp_path / "state" / "db", journal=True, sync=False)
        db.create_table("engine", TABLES["engine"])
        db.close()
        store = Store.open(tmp_path / "state", sync=False)
        store.apply(Batch(runs=[make_run()]))
        assert store.get_run(make_run().run_id) == make_run()
        store.close()


class TestMaxSeq:
    """The highest sequence number stored for a job and run date."""

    def test_max_seq(self, store: Store) -> None:
        """Return the highest seq of that job and date only, or 0."""
        runs = [
            make_run(seq=1),
            make_run(seq=3),
            make_run("load", seq=5),
            make_run(seq=9, run_date=NEXT_DAY),
        ]
        store.apply(Batch(runs=runs))
        assert store.max_seq("extract", DAY) == THREE
        assert store.max_seq("extract", PREV_DAY) == 0
        store.close()
