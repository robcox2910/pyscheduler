"""The engine test harness: a config, a store, a fake clock, a fake launcher, an engine."""

from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pynet.loop

from pyscheduler.config import Config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Run
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher

START = datetime(2026, 10, 19, tzinfo=UTC)
"""The instant every harness clock starts at: midnight UTC on Monday 19 October 2026."""

TODAY = date(2026, 10, 19)
"""The run date of jobs that fire on the first day."""

MAX_STEPS = 10_000
"""The most timer steps one ``advance_to`` takes before giving up."""

type TestBody = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class Harness:
    """Hold one engine and the fakes around it."""

    tmp_path: Path
    config: Config
    store: Store
    clock: FakeClock
    launcher: FakeLauncher
    engine: Engine

    async def advance_to(self, when: datetime) -> None:
        """Move the clock to ``when`` one timer at a time, settling the engine after each.

        The engine settles first too, so work an action just started (an
        attempt being launched) has set its timers before time moves.
        """
        await self.engine.settle()
        for _ in range(MAX_STEPS):
            due = self.clock.next_due()
            if due is None or due > when:
                break
            await self.clock.advance_to(due)
            await self.engine.settle()
        else:
            msg = "clock never caught up"
            raise RuntimeError(msg)
        await self.clock.advance_to(when)
        await self.engine.settle()

    async def advance(self, by: timedelta) -> None:
        """Move the clock forward by ``by`` (see ``advance_to``)."""
        await self.advance_to(self.clock.now() + by)

    def run_engine(self, body: TestBody) -> None:
        """Run ``body`` inside ``pynet.loop.run`` between engine start and stop."""

        async def main() -> None:
            """Start the engine, run the test body, stop the engine."""
            await self.engine.start()
            try:
                await self.engine.settle()
                await body()
            finally:
                await self.engine.stop()

        pynet.loop.run(main())

    @contextmanager
    def reopened(self) -> Generator[Store]:
        """Open the (stopped) engine's store again for reading, closing it afterwards."""
        store = Store.open(self.tmp_path / "state", sync=False)
        try:
            yield store
        finally:
            store.close()

    def run(self, job: str, run_date: date = TODAY) -> Run:
        """Return the latest run of ``job`` for ``run_date`` (it must exist)."""
        run = self.engine.book.latest_for(job, run_date)
        assert run is not None, f"no run of {job} for {run_date}"
        return run

    def runs(self, job: str, run_date: date = TODAY) -> list[Run]:
        """Return every run of ``job`` for ``run_date``, oldest first."""
        return self.engine.book.runs_for(job, run_date)


class MakeHarness(Protocol):
    """Build a harness from the YAML that follows the ``scheduler:`` section."""

    def __call__(self, text: str, *, settings: str = "") -> Harness:
        """Return a harness for ``text``; ``settings`` adds lines to ``scheduler:``."""
        ...
