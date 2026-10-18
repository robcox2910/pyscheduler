"""Test catch-up of missed firings, clock jumps, and pruning old history."""

import json
import logging
from datetime import timedelta

import pynet.loop
import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness
from recovery_helpers import make_run, restarted, run_at, seed

from pyscheduler.engine import recovery
from pyscheduler.engine.model import RunState, Trigger
from pyscheduler.store import Batch, ConfigRecord

ONE_HOUR = timedelta(hours=1)
FIVE_MINUTES = timedelta(minutes=5)
LATER = START + 3 * ONE_HOUR + 30 * timedelta(minutes=1)
TRUNCATED = 2
YESTERDAY = TODAY - timedelta(days=1)

HOURLY = """
jobs:
  h:
    command: run-h
    schedule: "0 * * * *"
    catch_up: {policy}
  off:
    command: run-off
    schedule: "0 * * * *"
    catch_up: all
    enabled: false
  m:
    command: run-m
    schedule: manual
"""


def last_tick_batch(at: object) -> Batch:
    """Return a batch that sets the stored ``last_tick``."""
    return Batch(engine={"last_tick": str(at)})


def config_record(h: Harness, jobs: str) -> ConfigRecord:
    """Return a stored config record whose only source names ``jobs``."""
    text = "jobs:\n" + "".join(
        f"  {name}: {{command: x, schedule: manual}}\n" for name in jobs.split()
    )
    return ConfigRecord(1, START, "digest", json.dumps({str(h.tmp_path / "old.yaml"): text}))


class TestCatchUp:
    """Missed firings follow each job's ``catch_up`` policy."""

    @pytest.mark.parametrize(("policy", "expected"), [("latest", 1), ("all", 3), ("none", 0)])
    def test_catch_up_policies(self, harness: MakeHarness, policy: str, expected: int) -> None:
        """Catch up 3 missed hourly firings as ``latest`` (1), ``all`` (3) or ``none`` (0)."""
        h = harness(HOURLY.format(policy=policy))
        seed(h, last_tick_batch(START.isoformat()))

        async def body() -> None:
            """Count the caught-up runs."""
            runs = h.runs("h")
            assert len(runs) == expected
            assert all(run.trigger is Trigger.CATCH_UP for run in runs)
            if runs:
                assert runs[-1].scheduled_at == START + 3 * ONE_HOUR
            assert h.runs("off") == []
            tick = await pynet.loop.run_in_thread(h.store.get_engine, "last_tick")
            assert tick == LATER.isoformat()
            await h.advance_to(START + 4 * ONE_HOUR)
            assert h.run("h").trigger is Trigger.SCHEDULE

        run_at(h, LATER, body)

    def test_new_job_not_caught_up(self, harness: MakeHarness) -> None:
        """Skip a job added while down (no runs, not in the last config record)."""
        h = harness(HOURLY.format(policy="all"))
        seed(h, Batch(engine={"last_tick": START.isoformat()}, configs=[config_record(h, "m")]))

        async def body() -> None:
            """Nothing caught up for ``h``."""
            assert h.runs("h") == []

        run_at(h, LATER, body)

    def test_known_or_busy_job_caught_up(self, harness: MakeHarness) -> None:
        """Catch up a job that was in the last config, or that has runs already."""
        h = harness(HOURLY.format(policy="latest"))
        old = make_run("h", RunState.SUCCEEDED, run_date=YESTERDAY)
        record = config_record(h, "m")
        seed(h, Batch(runs=[old], engine={"last_tick": START.isoformat()}, configs=[record]))

        async def body() -> None:
            """``h`` has runs, so it catches up."""
            assert len(h.runs("h")) == 1

        run_at(h, LATER, body)

    @pytest.mark.parametrize("sources", ["not json", "[]", '{"a.yaml": "jobs: [unclosed"}'])
    def test_unreadable_record_catches_up(self, harness: MakeHarness, sources: str) -> None:
        """Catch up as usual when the stored record cannot be read."""
        h = harness(HOURLY.format(policy="latest"))
        record = ConfigRecord(1, START, "digest", sources)
        seed(h, Batch(engine={"last_tick": START.isoformat()}, configs=[record]))

        async def body() -> None:
            """``h`` catches up its latest firing."""
            assert len(h.runs("h")) == 1

        run_at(h, LATER, body)

    def test_first_start_catches_nothing(self, harness: MakeHarness) -> None:
        """Catch nothing up without a ``last_tick``, but remember the start."""
        h = harness(HOURLY.format(policy="all"))

        async def body() -> None:
            """No runs, and ``last_tick`` is now."""
            assert h.runs("h") == []
            tick = await pynet.loop.run_in_thread(h.store.get_engine, "last_tick")
            assert tick == LATER.isoformat()

        run_at(h, LATER, body)

    def test_all_is_bounded(
        self,
        harness: MakeHarness,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Keep only the latest ``MAX_CATCH_UP`` firings, with a warning."""
        monkeypatch.setattr(recovery, "MAX_CATCH_UP", TRUNCATED)
        h = harness(HOURLY.format(policy="all"))
        seed(h, last_tick_batch(START.isoformat()))

        async def body() -> None:
            """Two runs: 02:00 and 03:00."""
            times = [run.scheduled_at for run in h.runs("h")]
            assert times == [START + 2 * ONE_HOUR, START + 3 * ONE_HOUR]

        with caplog.at_level(logging.WARNING):
            run_at(h, LATER, body)
        assert "missed 3 firings" in caplog.text

    def test_no_duplicate_after_restart(self, harness: MakeHarness) -> None:
        """Never create a firing twice, even when it is replayed after a restart."""
        h = harness(HOURLY.format(policy="all"))

        async def first() -> None:
            """Fire 00:00 and 01:00."""
            await h.advance_to(START + ONE_HOUR + FIVE_MINUTES)

        h.run_engine(first)
        again = restarted(h)
        seed(again, last_tick_batch((START - FIVE_MINUTES).isoformat()))

        async def second() -> None:
            """Nothing new for the replayed window."""
            assert [run.scheduled_at for run in again.runs("h")] == [START, START + ONE_HOUR]
            assert all(run.trigger is Trigger.SCHEDULE for run in again.runs("h"))

        again.run_engine(second)
        with again.reopened() as store:
            assert len(store.query_runs(job="h")) == len([START, START + ONE_HOUR])

    def test_clock_jump_applies_policy(
        self, harness: MakeHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Apply ``catch_up`` to firings a forward clock jump passed over."""
        h = harness(HOURLY.format(policy="latest"))
        jump = START + 5 * ONE_HOUR + FIVE_MINUTES

        async def body() -> None:
            """Jump the wall clock and wake the firing task."""
            await h.advance(FIVE_MINUTES)
            h.clock._now = jump
            with caplog.at_level(logging.WARNING):
                await h.advance_to(jump)
            assert "clock jump: h was due" in caplog.text
            runs = h.runs("h")
            assert [(r.scheduled_at, r.trigger) for r in runs] == [
                (START, Trigger.SCHEDULE),
                (START + 5 * ONE_HOUR, Trigger.CATCH_UP),
            ]
            await h.advance_to(START + 6 * ONE_HOUR)
            assert h.run("h").trigger is Trigger.SCHEDULE

        h.run_engine(body)
