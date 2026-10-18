"""Test admission control: the global cap, pools and the ready queue."""

from datetime import UTC, date, datetime, timedelta

import pytest

from pyscheduler.config.model import Criticality
from pyscheduler.engine.admission import Admission, ReadyQueue
from pyscheduler.engine.model import Run, RunState, Trigger

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
NO_NEEDS: dict[str, int] = {}
DB = {"db": 1}
TWO = 2
THREE = 3


def make_run(run_id: str, minutes: int = 0) -> Run:
    """Build a queued run scheduled ``minutes`` after T0."""
    return Run(
        run_id=run_id,
        job=run_id,
        run_date=date(2026, 10, 19),
        scheduled_at=T0 + timedelta(minutes=minutes),
        state=RunState.QUEUED,
        reason="",
        attempt=0,
        rerun=0,
        trigger=Trigger.SCHEDULE,
        config_version=1,
        created_at=T0,
        updated_at=T0,
    )


def ids(runs: list[Run]) -> list[str]:
    """Return the run ids of ``runs`` in order."""
    return [r.run_id for r in runs]


class TestAdmission:
    """Check slot counting."""

    def test_fits_and_acquire(self) -> None:
        """Acquire counts usage and release frees it."""
        adm = Admission(THREE, {"db": 1})
        assert adm.fits(DB)
        adm.acquire(DB)
        assert adm.running == 1
        assert adm.in_use("db") == 1
        assert not adm.fits(DB)
        assert adm.fits(NO_NEEDS)
        adm.release(DB)
        assert adm.running == 0
        assert adm.in_use("db") == 0

    def test_acquire_when_full_raises(self) -> None:
        """Acquire refuses what does not fit."""
        adm = Admission(1, {})
        adm.acquire(NO_NEEDS)
        with pytest.raises(ValueError, match="fit"):
            adm.acquire(NO_NEEDS)

    def test_unknown_pool(self) -> None:
        """An unknown pool never fits."""
        adm = Admission(THREE, {})
        assert not adm.fits({"x": 1})
        with pytest.raises(ValueError, match="fit"):
            adm.acquire({"x": 1})

    def test_bad_need_amount(self) -> None:
        """Needs below one are rejected."""
        adm = Admission(THREE, {"db": TWO})
        with pytest.raises(ValueError, match="at least 1"):
            adm.fits({"db": 0})

    def test_over_release(self) -> None:
        """Releasing more than was taken is a bug and raises."""
        adm = Admission(THREE, {"db": TWO})
        with pytest.raises(ValueError, match="release"):
            adm.release(NO_NEEDS)
        with pytest.raises(ValueError, match="release"):
            adm.release(DB)

    def test_set_limits_shrink(self) -> None:
        """Shrinking below usage blocks new starts until usage drops."""
        adm = Admission(THREE, {"db": TWO})
        adm.acquire(DB)
        adm.acquire(DB)
        adm.set_limits(3, {"db": 1})
        assert adm.in_use("db") == TWO
        assert not adm.fits(DB)
        adm.release(DB)
        assert not adm.fits(DB)
        adm.release(DB)
        assert adm.fits(DB)

    def test_set_limits_global(self) -> None:
        """A lower global cap blocks starts too."""
        adm = Admission(THREE, {})
        adm.acquire(NO_NEEDS)
        adm.acquire(NO_NEEDS)
        adm.set_limits(1, {})
        assert not adm.fits(NO_NEEDS)


class TestReadyQueue:
    """Check queue order and admission."""

    def test_priority_order(self) -> None:
        """A later critical run starts before an earlier normal one."""
        q = ReadyQueue()
        q.push(make_run("normal", 0), Criticality.NORMAL, NO_NEEDS)
        q.push(make_run("crit", 10), Criticality.CRITICAL, NO_NEEDS)
        started = q.take_startable(Admission(1, {}))
        assert ids(started) == ["crit"]
        assert "normal" in q

    def test_tie_breaks(self) -> None:
        """Equal criticality goes by time, then run id."""
        q = ReadyQueue()
        q.push(make_run("b", 5), Criticality.NORMAL, NO_NEEDS)
        q.push(make_run("c", 0), Criticality.NORMAL, NO_NEEDS)
        q.push(make_run("a", 5), Criticality.NORMAL, NO_NEEDS)
        assert ids(q.take_startable(Admission(9, {}))) == ["c", "a", "b"]

    def test_busy_pool_does_not_block_others(self) -> None:
        """A run stuck on a full pool does not hold up the ones behind it."""
        adm = Admission(THREE, {"db": 1})
        adm.acquire(DB)
        q = ReadyQueue()
        q.push(make_run("dbjob", 0), Criticality.NORMAL, DB)
        q.push(make_run("plain", 1), Criticality.NORMAL, NO_NEEDS)
        assert ids(q.take_startable(adm)) == ["plain"]
        assert "dbjob" in q
        assert len(q) == 1

    def test_global_cap(self) -> None:
        """Only max_running runs start."""
        adm = Admission(TWO, {})
        q = ReadyQueue()
        for i in range(THREE):
            q.push(make_run(f"r{i}", i), Criticality.NORMAL, NO_NEEDS)
        assert len(q.take_startable(adm)) == TWO
        assert adm.running == TWO
        assert len(q) == 1

    def test_release_then_take(self) -> None:
        """Freeing a slot lets the waiting run start."""
        adm = Admission(1, {})
        q = ReadyQueue()
        q.push(make_run("a", 0), Criticality.NORMAL, NO_NEEDS)
        q.push(make_run("b", 1), Criticality.NORMAL, NO_NEEDS)
        assert ids(q.take_startable(adm)) == ["a"]
        assert q.take_startable(adm) == []
        adm.release(NO_NEEDS)
        assert ids(q.take_startable(adm)) == ["b"]
        assert len(q) == 0

    def test_remove(self) -> None:
        """Remove drops a queued run and reports absence."""
        q = ReadyQueue()
        q.push(make_run("a", 0), Criticality.NORMAL, NO_NEEDS)
        assert q.remove("a")
        assert not q.remove("a")
        assert "a" not in q
        assert len(q) == 0
        assert q.take_startable(Admission(THREE, {})) == []

    def test_push_replaces(self) -> None:
        """Pushing a queued run id replaces its entry."""
        q = ReadyQueue()
        q.push(make_run("a", 0), Criticality.LOW, NO_NEEDS)
        q.push(make_run("b", 1), Criticality.NORMAL, NO_NEEDS)
        q.push(make_run("a", 0), Criticality.CRITICAL, NO_NEEDS)
        assert len(q) == TWO
        assert ids(q.take_startable(Admission(9, {}))) == ["a", "b"]

    def test_force_jumps_queue(self) -> None:
        """Forced runs go first but must still fit."""
        q = ReadyQueue()
        q.push(make_run("hi", 0), Criticality.CRITICAL, NO_NEEDS)
        q.push(make_run("forced", 9), Criticality.LOW, NO_NEEDS)
        started = q.take_startable(Admission(1, {}), force=frozenset({"forced"}))
        assert ids(started) == ["forced"]
        assert "hi" in q

    def test_force_still_must_fit(self) -> None:
        """A forced run on a full pool stays queued."""
        adm = Admission(THREE, {"db": 1})
        adm.acquire(DB)
        q = ReadyQueue()
        q.push(make_run("forced", 0), Criticality.LOW, DB)
        q.push(make_run("other", 1), Criticality.LOW, NO_NEEDS)
        started = q.take_startable(adm, force=frozenset({"forced", "gone"}))
        assert ids(started) == ["other"]
        assert "forced" in q
        adm.release(DB)
        assert ids(q.take_startable(adm, force=frozenset({"forced"}))) == ["forced"]

    def test_skipped_entries_survive(self) -> None:
        """Skipped entries keep their place for the next call."""
        adm = Admission(THREE, {"db": 1})
        adm.acquire(DB)
        q = ReadyQueue()
        q.push(make_run("d1", 0), Criticality.NORMAL, DB)
        q.push(make_run("d2", 1), Criticality.NORMAL, DB)
        assert q.take_startable(adm) == []
        adm.release(DB)
        assert ids(q.take_startable(adm)) == ["d1"]
        assert "d2" in q
