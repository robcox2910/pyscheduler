"""Upstream runs a dependency offset reaches stay in memory (ruling R27)."""

from datetime import timedelta

from engine_helpers import START, TODAY, MakeHarness

from pyscheduler.engine.deps import dependency_reach
from pyscheduler.engine.model import RunState

ONE_DAY = timedelta(days=1)
DAYS = 12
"""How many days the probe runs for."""

WEEK_BACK = """
jobs:
  a: {command: run-a, schedule: "0 1 * * *"}
  b:
    command: run-b
    schedule: "0 2 * * *"
    depends_on:
      - {job: a, run_date: -7}
"""

DAY_BACK = """
jobs:
  a: {command: run-a, schedule: "0 1 * * *"}
  b:
    command: run-b
    schedule: "0 2 * * *"
    depends_on:
      - {job: a, run_date: -1}
"""

SLACK = 7
"""The days added to the reach for weekends and holidays."""

WEEK = 7


class TestReach:
    """``dependency_reach`` measures the furthest an offset looks back."""

    def test_reach_of_offsets(self, harness: MakeHarness) -> None:
        """Seven days back on an all-days calendar, plus a week of slack; none for offset 0."""
        assert dependency_reach(harness(WEEK_BACK).config, TODAY) == WEEK + SLACK
        plain = "jobs:\n  a: {command: x, schedule: manual}\n  b: {command: y, depends_on: [a]}\n"
        assert dependency_reach(harness(plain).config, TODAY) == 0


class TestOffsetsBeyondMemory:
    """A new run still sees an upstream run older than ``memory_days``."""

    def _probe(self, harness: MakeHarness, text: str, settings: str, back: int) -> None:
        """Run ``DAYS`` days; every ``b`` whose upstream day exists must succeed."""
        h = harness(text, settings=settings)

        async def body() -> None:
            """Let the days go by, pruning every hour as usual."""
            await h.advance_to(START + DAYS * ONE_DAY)

        h.run_engine(body)
        with h.reopened() as store:  # older runs have left memory by now: ask the store
            for n in range(back, DAYS):
                day = TODAY + n * ONE_DAY
                run = store.get_run(f"b.{day.isoformat()}.1")
                assert run is not None, day
                assert run.state is RunState.SUCCEEDED, (day, run.reason)

    def test_week_back_with_default_memory(self, harness: MakeHarness) -> None:
        """``run_date: -7`` with ``memory_days: 7``: every ``b`` from day 7 on runs."""
        self._probe(harness, WEEK_BACK, "", WEEK)

    def test_one_day_memory(self, harness: MakeHarness) -> None:
        """``run_date: -1`` with ``memory_days: 1``: every ``b`` from day 1 on runs."""
        self._probe(harness, DAY_BACK, "memory_days: 1", 1)
