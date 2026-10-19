"""Tests for the batched attempts read: many runs' attempts in one locked call."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from store_helpers import T0, make_attempt, make_run

from pyscheduler.engine.model import Outcome
from pyscheduler.store import Batch, Store

MINUTE = timedelta(minutes=1)
RUNS = 20


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Open a fresh store without fsync."""
    return Store.open(tmp_path / "state", sync=False)


class TestAttemptsForRuns:
    """``attempts_for_runs`` answers for every run asked, under one lock."""

    def test_attempts_by_run_in_order(self, store: Store) -> None:
        """Each run gets its attempts, first attempt first; runs without any get ``[]``."""
        first, second, bare = (make_run(seq=n) for n in (1, 2, 3))
        attempts = [
            make_attempt(first.run_id, 2, started_at=T0 + MINUTE, took=MINUTE),
            make_attempt(first.run_id, 1, took=MINUTE, outcome=Outcome.FAILED),
            make_attempt(second.run_id, took=MINUTE, outcome=Outcome.SUCCEEDED),
        ]
        store.apply(Batch(runs=[first, second, bare], attempts=attempts))
        found = store.attempts_for_runs([first.run_id, second.run_id, bare.run_id, "nope"])
        assert [a.attempt for a in found[first.run_id]] == [1, 2]
        assert [a.attempt for a in found[second.run_id]] == [1]
        assert found[bare.run_id] == []
        assert found["nope"] == []
        assert store.attempts_for_runs([]) == {}
        store.close()

    def test_takes_the_lock_once(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        """Asking for many runs enters the database lock once."""
        entered: list[int] = []
        real = store._db.lock  # type: ignore[reportPrivateUsage]

        class Counting:
            """Count how often the lock is entered."""

            def __enter__(self) -> None:
                """Enter the real lock and count it."""
                entered.append(1)
                real.__enter__()

            def __exit__(self, *exc: Any) -> None:
                """Leave the real lock."""
                real.__exit__(*exc)

        class Proxy:
            """Stand in for the database, with a counting lock."""

            lock = Counting()

        monkeypatch.setattr(store, "_db", Proxy())
        store.attempts_for_runs([f"run{n}" for n in range(RUNS)])
        assert len(entered) == 1
        monkeypatch.undo()
        store.close()
