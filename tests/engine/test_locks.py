"""Test the per-run locks: waiting in line, trying without waiting, cancelling."""

import pynet.loop
import pytest
from pynet.loop import Cancelled

from pyscheduler.engine.locks import RunLocks

RUN = "a.2026-10-19.1"


class TestRunLocks:
    """One key per run, made lazily and dropped when idle."""

    def test_try_take_and_give_back(self) -> None:
        """Take a free lock at once, refuse a held one, and drop it when idle."""
        locks = RunLocks()
        assert locks.try_take(RUN)
        assert not locks.try_take(RUN)
        locks.give_back(RUN)
        assert len(locks) == 0
        with pytest.raises(RuntimeError):
            locks.give_back(RUN)

    def test_waiters_in_order_and_cancelled_waiter(self) -> None:
        """Hand the lock to waiters first come, first served, skipping a cancelled one."""
        order: list[str] = []

        async def main() -> None:
            """Hold the lock while three tasks queue for it; cancel the middle one."""
            locks = RunLocks()
            await locks.take(RUN)

            async def wait(name: str) -> None:
                """Take the lock, note the name, give it back."""
                async with locks.hold(RUN):
                    order.append(name)

            first = pynet.loop.spawn(wait("first"))
            middle = pynet.loop.spawn(wait("middle"))
            last = pynet.loop.spawn(wait("last"))
            await pynet.loop.sleep(0)
            assert not locks.try_take(RUN)
            middle.cancel()
            await pynet.loop.sleep(0)
            locks.give_back(RUN)
            await first
            await last
            with pytest.raises(Cancelled):
                await middle
            await locks.wait_free(RUN)
            assert len(locks) == 0

        pynet.loop.run(main())
        assert order == ["first", "last"]
