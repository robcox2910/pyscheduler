"""Tests for the clocks: the real wall clock and the fake one tests wind by hand."""

import time
from datetime import UTC, datetime, timedelta

import pynet.loop
import pytest

from pyscheduler.engine import clock as clock_module
from pyscheduler.engine.clock import Clock, FakeClock, SystemClock

START = datetime(2026, 10, 19, tzinfo=UTC)
SHORT_DELAY = 0.05
TINY_NAP = 0.01
SLEEP_SECONDS = 5


class TestFakeClock:
    """The fake clock moves only when a test advances it."""

    def test_fake_clock_orders_callbacks(self) -> None:
        """Fire callbacks in time order, with ``now`` at each callback's time."""
        clock = FakeClock(START)
        seen: list[tuple[str, datetime]] = []

        async def main() -> None:
            """Schedule two callbacks out of order and advance past both."""
            clock.call_at(START + timedelta(seconds=2), lambda: seen.append(("two", clock.now())))
            clock.call_at(START + timedelta(seconds=1), lambda: seen.append(("one", clock.now())))
            await clock.advance(timedelta(seconds=3))

        pynet.loop.run(main())
        assert seen == [
            ("one", START + timedelta(seconds=1)),
            ("two", START + timedelta(seconds=2)),
        ]
        assert clock.now() == START + timedelta(seconds=3)

    def test_ties_fire_in_arrival_order(self) -> None:
        """Fire callbacks due at the same instant in the order they were added."""
        clock = FakeClock(START)
        seen: list[int] = []
        when = START + timedelta(seconds=1)

        async def main() -> None:
            """Add three callbacks for one instant."""
            for n in range(3):
                clock.call_at(when, lambda n=n: seen.append(n))
            await clock.advance(timedelta(seconds=1))

        pynet.loop.run(main())
        assert seen == [0, 1, 2]

    def test_fake_clock_sleep_until_wakes_task(self) -> None:
        """Wake a task sleeping until t+5 after 5 s, not after 4 s."""
        clock = FakeClock(START)
        woke: list[datetime] = []

        async def sleeper() -> None:
            """Sleep until t+5 and note when we woke."""
            await clock.sleep_until(START + timedelta(seconds=SLEEP_SECONDS))
            woke.append(clock.now())

        async def main() -> None:
            """Advance 4 s, check, then 1 s more."""
            pynet.loop.spawn(sleeper())
            await pynet.loop.sleep(0)
            await clock.advance(timedelta(seconds=SLEEP_SECONDS - 1))
            assert woke == []
            await clock.advance(timedelta(seconds=1))
            assert woke == [START + timedelta(seconds=SLEEP_SECONDS)]

        pynet.loop.run(main())

    def test_sleep_until_past_returns_at_once(self) -> None:
        """Return straight away when the instant has already passed."""
        clock = FakeClock(START)

        async def main() -> None:
            """Sleep until a past instant."""
            await clock.sleep_until(START - timedelta(seconds=1))

        pynet.loop.run(main())

    def test_cancelled_callback_never_fires(self) -> None:
        """Skip a callback whose cancel was called."""
        clock = FakeClock(START)
        seen: list[str] = []

        async def main() -> None:
            """Schedule, cancel, advance."""
            handle = clock.call_at(START + timedelta(seconds=1), lambda: seen.append("x"))
            handle.cancel()
            await clock.advance(timedelta(seconds=2))

        pynet.loop.run(main())
        assert seen == []

    def test_callbacks_added_during_advance_fire_inside_window(self) -> None:
        """Fire a callback that a callback scheduled within the same advance."""
        clock = FakeClock(START)
        seen: list[datetime] = []

        def first() -> None:
            """Note the time and schedule a follow-up one second later."""
            seen.append(clock.now())
            clock.call_at(clock.now() + timedelta(seconds=1), lambda: seen.append(clock.now()))

        async def main() -> None:
            """Advance far enough for both."""
            clock.call_at(START + timedelta(seconds=1), first)
            await clock.advance_to(START + timedelta(seconds=10))

        pynet.loop.run(main())
        assert seen == [START + timedelta(seconds=1), START + timedelta(seconds=2)]

    def test_due_callback_fires_without_advance(self) -> None:
        """Run a callback for ``now`` (or earlier) on the next loop turns."""
        clock = FakeClock(START)
        seen: list[str] = []

        async def main() -> None:
            """Schedule for now and just yield."""
            clock.call_at(START, lambda: seen.append("now"))
            for _ in range(clock_module.SETTLE_TURNS):
                await pynet.loop.sleep(0)

        pynet.loop.run(main())
        assert seen == ["now"]

    def test_advance_to_never_goes_backwards(self) -> None:
        """Leave ``now`` alone when asked to advance to the past."""
        clock = FakeClock(START)

        async def main() -> None:
            """Advance to an earlier instant."""
            await clock.advance_to(START - timedelta(hours=1))

        pynet.loop.run(main())
        assert clock.now() == START

    def test_rejects_naive_start(self) -> None:
        """Refuse a start time with no time zone."""
        with pytest.raises(ValueError, match="aware"):
            FakeClock(datetime(2026, 10, 19))  # noqa: DTZ001


class TestSystemClock:
    """The real clock uses the wall clock and the running pynet loop."""

    def test_now_is_aware_utc(self) -> None:
        """Return an aware UTC datetime."""
        assert SystemClock().now().tzinfo is UTC

    def test_satisfies_protocol(self) -> None:
        """Both clocks can stand where a ``Clock`` is wanted."""
        clocks: list[Clock] = [SystemClock(), FakeClock(START)]
        assert len(clocks) == len({id(c) for c in clocks})

    def test_system_clock_call_at_short(self) -> None:
        """Fire a real-time callback 0.05 s from now inside ``pynet.loop.run``."""
        clock = SystemClock()
        fired: list[datetime] = []

        async def main() -> None:
            """Schedule a short callback and wait for it."""
            when = clock.now() + timedelta(seconds=SHORT_DELAY)
            done = pynet.loop.Event()

            def ring() -> None:
                """Note the time and wake the test."""
                fired.append(clock.now())
                done.set()

            clock.call_at(when, ring)
            async with pynet.loop.timeout(2):
                await done.wait()
            assert fired[0] >= when

        pynet.loop.run(main())
        assert len(fired) == 1

    def test_rearms_in_naps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-arm in short naps when the wait is longer than ``MAX_NAP``."""
        monkeypatch.setattr(clock_module, "MAX_NAP", TINY_NAP)
        clock = SystemClock()

        async def main() -> None:
            """Sleep across several naps."""
            when = clock.now() + timedelta(seconds=SHORT_DELAY)
            await clock.sleep_until(when)
            assert clock.now() >= when

        pynet.loop.run(main())

    def test_cancel_stops_callback(self) -> None:
        """Never fire a callback that was cancelled."""
        clock = SystemClock()
        seen: list[str] = []

        async def main() -> None:
            """Schedule, cancel, then wait past the time."""
            handle = clock.call_at(
                clock.now() + timedelta(seconds=TINY_NAP), lambda: seen.append("x")
            )
            handle.cancel()
            await pynet.loop.sleep(SHORT_DELAY)

        pynet.loop.run(main())
        assert seen == []

    def test_sleep_until_past(self) -> None:
        """Return at once for an instant already gone."""
        clock = SystemClock()

        async def main() -> None:
            """Sleep until a second ago."""
            started = time.monotonic()
            await clock.sleep_until(clock.now() - timedelta(seconds=1))
            assert time.monotonic() - started < 1

        pynet.loop.run(main())
