"""Tests for the batched duration read: many jobs' recent run times in one pass."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from store_helpers import T0, make_attempt, make_run

from pyscheduler.engine.model import Outcome
from pyscheduler.store import Batch, Store

MINUTE = timedelta(minutes=1)
LIMIT_TWO = 2
JOBS = 20


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Open a fresh store without fsync."""
    return Store.open(tmp_path / "state", sync=False)


class TestRecentDurationsFor:
    """``recent_durations_for`` answers for every job asked, under one lock."""

    def test_many_jobs_in_one_pass(self, store: Store) -> None:
        """Each job gets its succeeded durations, newest end first; unknown jobs get ``[]``."""
        runs = [make_run(seq=n) for n in (1, 2, 3)]
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
        ]
        other = make_run(job="other")
        other_attempt = make_attempt(other.run_id, took=9 * MINUTE, outcome=Outcome.SUCCEEDED)
        store.apply(Batch(runs=[*runs, other], attempts=[*attempts, other_attempt]))
        found = store.recent_durations_for(["extract", "other", "missing"])
        assert found == {
            "extract": [2 * MINUTE, 1 * MINUTE],
            "other": [9 * MINUTE],
            "missing": [],
        }
        assert store.recent_durations_for(["extract"], limit=1) == {"extract": [2 * MINUTE]}
        assert store.recent_durations_for([]) == {}
        assert store.recent_durations("extract", limit=LIMIT_TWO) == [2 * MINUTE, 1 * MINUTE]
        store.close()

    def test_takes_the_lock_once(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        """Asking for many jobs enters the database lock once, not once per job."""
        db: Any = store._db
        entered: list[int] = []
        real = db.lock

        class Counting:
            """Count how often the lock is entered."""

            def __enter__(self) -> None:
                """Enter the real lock and count it."""
                entered.append(1)
                real.__enter__()

            def __exit__(self, *exc: object) -> None:
                """Leave the real lock."""
                real.__exit__(*exc)

        class Proxy:
            """Stand in for the database, with a counting lock."""

            lock = Counting()

        monkeypatch.setattr(store, "_db", Proxy())
        store.recent_durations_for([f"job{n}" for n in range(JOBS)])
        assert len(entered) == 1
        monkeypatch.undo()
        store.close()
