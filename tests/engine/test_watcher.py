"""Tests for the file watcher: polling a glob until a match sits still."""

import signal
import threading
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pynet.loop
import pytest
from pynet.loop import Semaphore, Task

from pyscheduler.config.model import WatchSpec
from pyscheduler.engine import watcher as watcher_module
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.status_note import AttemptResult
from pyscheduler.engine.watcher import WatcherHandle, stat_matches

START = datetime(2026, 10, 19, tzinfo=UTC)
POLL = timedelta(seconds=10)
MIN_AGE = timedelta(seconds=30)
GIVE_UP = timedelta(minutes=1)
SETTLE_NAP = 0.002
HANDLES = 5
LIMIT = 2
SHORT_STAT = 0.02
LATE_POLLS = 5
GROWTHS = 3
PAST = timedelta(seconds=50)
STEP = timedelta(seconds=1)
HOUR = 3600


def make(
    clock: FakeClock,
    tmp_path: Path,
    pattern: str,
    *,
    started_at: datetime = START,
    limit: Semaphore | None = None,
    give_up_after: timedelta = GIVE_UP,
) -> WatcherHandle:
    """Build a handle watching ``pattern`` with the standard spec."""
    spec = WatchSpec(pattern, MIN_AGE, POLL, give_up_after)
    return WatcherHandle(
        pattern=pattern,
        spec=spec,
        started_at=started_at,
        clock=clock,
        limit=limit or Semaphore(LIMIT),
        log_path=tmp_path / "logs" / "watch.log",
    )


async def settle(clock: FakeClock, tasks: list[Task[AttemptResult]]) -> None:
    """Yield until every task is done or sleeping on the clock again."""
    while not all(t.done() for t in tasks) and clock.next_due() is None:
        await pynet.loop.sleep(SETTLE_NAP)


async def run_until(
    clock: FakeClock,
    tasks: list[Task[AttemptResult]],
    stop: datetime,
    before_poll: Callable[[datetime], None] | None = None,
) -> None:
    """Step the clock one timer at a time up to ``stop``, letting threads finish each time."""
    await settle(clock, tasks)
    while (due := clock.next_due()) is not None and due <= stop:
        if before_poll is not None:
            before_poll(due)
        await clock.advance_to(due)
        await settle(clock, tasks)
        if all(t.done() for t in tasks):
            break


def run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run ``body`` on a fresh loop."""
    pynet.loop.run(body())


class TestStatMatches:
    """The thread-side glob and stat."""

    def test_regular_files_only(self, tmp_path: Path) -> None:
        """Map files to (size, mtime_ns) and skip directories."""
        (tmp_path / "a.csv").write_text("abc")
        (tmp_path / "dir.csv").mkdir()
        found = stat_matches(str(tmp_path / "*.csv"))
        assert list(found) == [str(tmp_path / "a.csv")]
        assert found[str(tmp_path / "a.csv")][0] == len("abc")

    def test_vanished_file_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skip a path whose stat fails after the glob saw it."""

        def fake_glob(_pattern: str) -> list[str]:
            """Report a path that does not exist."""
            return [str(tmp_path / "gone")]

        monkeypatch.setattr(watcher_module.glob, "glob", fake_glob)
        assert stat_matches("x") == {}


class TestWatcherHandle:
    """Polling, stability, deadlines and stopping."""

    def test_finds_stable_file(self, tmp_path: Path) -> None:
        """Succeed once the file has been still for min_age, not before."""
        target = tmp_path / "in.csv"
        target.write_text("x")

        async def main() -> None:
            """Advance to +20s (nothing), then +30s (found)."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + timedelta(seconds=20))
            assert not task.done()
            await run_until(clock, [task], START + MIN_AGE)
            result = await task
            assert result.succeeded
            assert result.signal is None
            assert clock.now() == START + MIN_AGE
            assert handle.files == (str(target),)
            assert handle.pid is None
            log = (tmp_path / "logs" / "watch.log").read_text()
            assert "1 match(es)" in log
            assert f"found {target}" in log

        run(main)

    def test_growing_file_waits_until_stable(self, tmp_path: Path) -> None:
        """Restart the clock on every change: success is min_age after the last one."""
        target = tmp_path / "in.csv"
        target.write_text("x")
        polls = {"n": 0}

        def grow(_due: datetime) -> None:
            """Append a byte before each of the next polls."""
            polls["n"] += 1
            if polls["n"] <= GROWTHS - 1:
                with target.open("a") as handle:
                    handle.write("x")

        async def main() -> None:
            """Poll at 0 (initial), then grow before the polls at 10 and 20."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + timedelta(minutes=5), grow)
            await task
            assert clock.now() == START + timedelta(seconds=20) + MIN_AGE
            assert handle.files == (str(target),)

        run(main)

    def test_file_appears_late(self, tmp_path: Path) -> None:
        """Find a file min_age after it first appears."""
        target = tmp_path / "late.csv"
        count = {"n": 0}

        def maybe_create(_due: datetime) -> None:
            """Create the file just before the sixth poll."""
            count["n"] += 1
            if count["n"] == LATE_POLLS:
                target.write_text("x")

        async def main() -> None:
            """Wait for the file to appear and age."""
            clock = FakeClock(START)
            handle = make(
                clock, tmp_path, str(tmp_path / "*.csv"), give_up_after=timedelta(hours=1)
            )
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + timedelta(hours=1), maybe_create)
            await task
            assert clock.now() == START + LATE_POLLS * POLL + MIN_AGE
            assert handle.files == (str(target),)

        run(main)

    def test_glob_returns_all_stable_matches_sorted(self, tmp_path: Path) -> None:
        """Return every stable match in sorted order and ignore directories."""
        (tmp_path / "b.csv").write_text("b")
        (tmp_path / "a.csv").write_text("a")
        (tmp_path / "dir.csv").mkdir()

        async def main() -> None:
            """Watch the glob until both files are stable."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + GIVE_UP)
            await task
            assert handle.files == (str(tmp_path / "a.csv"), str(tmp_path / "b.csv"))

        run(main)

    def test_gives_up(self, tmp_path: Path) -> None:
        """Give up at the deadline when nothing ever matches."""

        async def main() -> None:
            """Run to the deadline."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + GIVE_UP)
            result = await task
            assert handle.gave_up is True
            assert result.exit_code is None
            assert result.signal is None
            assert result.start_error is None
            assert clock.now() == START + GIVE_UP
            assert handle.files == ()

        run(main)

    def test_started_at_in_the_past_keeps_deadline(self, tmp_path: Path) -> None:
        """Count the deadline from started_at, not from the first poll."""

        async def main() -> None:
            """Start 50s late with a 1m deadline: give up 10s in."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"), started_at=START - PAST)
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + GIVE_UP)
            await task
            assert handle.gave_up is True
            assert clock.now() == START + POLL

        run(main)

    def test_limit_is_shared(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never run more stat calls at once than the shared semaphore allows."""
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def counting(_pattern: str) -> dict[str, tuple[int, int]]:
            """Track how many calls overlap."""
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(SHORT_STAT)
            with lock:
                state["now"] -= 1
            return {}

        monkeypatch.setattr(watcher_module, "stat_matches", counting)

        async def main() -> None:
            """Run five watchers on one Semaphore(2) to their deadlines."""
            clock = FakeClock(START)
            limit = Semaphore(LIMIT)
            handles = [
                make(clock, tmp_path, "x", limit=limit, give_up_after=POLL) for _ in range(HANDLES)
            ]
            tasks = [pynet.loop.spawn(h.wait()) for h in handles]
            await run_until(clock, tasks, START + POLL)
            await pynet.loop.gather(*tasks)
            assert all(h.gave_up for h in handles)
            assert state["peak"] == LIMIT

        run(main)

    @pytest.mark.parametrize("stop", ["terminate", "kill"])
    def test_terminate_stops_polling(self, tmp_path: Path, stop: str) -> None:
        """Return a SIGTERM result at once, even mid-sleep."""

        async def main() -> None:
            """Stop a sleeping watcher."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            task = pynet.loop.spawn(handle.wait())
            await settle(clock, [task])
            getattr(handle, stop)()
            result = await task
            assert result.signal == signal.SIGTERM
            assert result.exit_code is None
            assert not handle.gave_up

        run(main)

    def test_terminate_while_waiting_for_the_limit(self, tmp_path: Path) -> None:
        """Stop a watcher that is queued on a full semaphore."""

        async def main() -> None:
            """Hold the only permit, then terminate the queued watcher."""
            clock = FakeClock(START)
            limit = Semaphore(1)
            await limit.acquire()
            handle = make(clock, tmp_path, "x", limit=limit)
            task = pynet.loop.spawn(handle.wait())
            await pynet.loop.sleep(SETTLE_NAP)
            handle.terminate()
            result = await task
            assert result.signal == signal.SIGTERM

        run(main)

    def test_outer_cancel_cancels_polling(self, tmp_path: Path) -> None:
        """Cancel the poll when the task waiting on it is cancelled."""

        async def main() -> None:
            """Cancel the waiter and see the clock timer go away."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, "x")
            task = pynet.loop.spawn(handle.wait())
            await settle(clock, [task])
            task.cancel()
            await pynet.loop.sleep(SETTLE_NAP)
            await pynet.loop.sleep(SETTLE_NAP)
            assert task.cancelled()
            assert clock.next_due() is None

        run(main)

    def test_terminate_before_wait(self, tmp_path: Path) -> None:
        """Return SIGTERM at once, with no polling, when stopped before waiting."""

        async def main() -> None:
            """Stop first, then wait."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            handle.terminate()
            result = await handle.wait()
            assert result.signal == signal.SIGTERM
            assert clock.next_due() is None
            assert clock.now() == START

        run(main)

    def test_unwritable_log_does_not_stop_the_watch(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warn and keep polling when the log cannot be written."""
        (tmp_path / "in.csv").write_text("x")
        blocker = tmp_path / "logs"
        blocker.write_text("not a folder")

        async def main() -> None:
            """Watch with a log path under a plain file."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"))
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + GIVE_UP)
            assert (await task).succeeded

        with caplog.at_level("WARNING", logger="pyscheduler.engine.watcher"):
            run(main)
        assert "cannot write watcher log" in caplog.text

    def test_file_stable_exactly_at_the_deadline(self, tmp_path: Path) -> None:
        """Poll once more at the deadline, so min_age == give_up_after still succeeds."""
        (tmp_path / "in.csv").write_text("x")

        async def main() -> None:
            """Watch with min_age equal to the give-up time."""
            clock = FakeClock(START)
            handle = make(clock, tmp_path, str(tmp_path / "*.csv"), give_up_after=MIN_AGE)
            task = pynet.loop.spawn(handle.wait())
            await run_until(clock, [task], START + MIN_AGE)
            assert (await task).succeeded
            assert not handle.gave_up

        run(main)


class _LateClock(FakeClock):
    """A fake clock whose reading lags one step behind once ``late_from`` has come."""

    def __init__(self, start: datetime, late_from: datetime) -> None:
        """Start at ``start`` and read late from ``late_from`` on."""
        super().__init__(start)
        self._late_from = late_from

    def now(self) -> datetime:
        """Return the time, one step later once the late time has come (a slow final look)."""
        now = super().now()
        return now + STEP if now >= self._late_from else now


class TestFinalLook:
    """The last look before giving up judges stillness at the time of the look."""

    def test_late_final_look_still_finds_a_zero_age_file(self, tmp_path: Path) -> None:
        """A file first seen at a final look that runs past the deadline is still found."""
        target = tmp_path / "in.csv"
        deadline = START + GIVE_UP

        async def main() -> None:
            """Create the file just before the final look, which reads the clock late."""
            clock = _LateClock(START, deadline)
            spec = WatchSpec(str(target), timedelta(0), POLL, GIVE_UP)
            handle = WatcherHandle(
                pattern=str(target),
                spec=spec,
                started_at=START,
                clock=clock,
                limit=Semaphore(LIMIT),
                log_path=tmp_path / "logs" / "watch.log",
            )
            task = pynet.loop.spawn(handle.wait())

            def before_poll(due: datetime) -> None:
                """Drop the file in right before the look at the deadline."""
                if due >= deadline:
                    target.write_text("x")

            await run_until(clock, [task], deadline, before_poll)
            result = await task
            assert result.exit_code == 0
            assert handle.files == (str(target),)
            assert handle.gave_up is False

        run(main)
