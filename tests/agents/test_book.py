"""Tests for the agent book: liveness, load, placement and watching."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pynet.loop

from pyscheduler.agents.book import AgentBook, AgentState, Refusal
from pyscheduler.engine.clock import FakeClock

START = datetime(2026, 10, 20, tzinfo=UTC)
GRACE = timedelta(seconds=60)
FIFTY = timedelta(seconds=50)
TEN = timedelta(seconds=10)
SLOTS = 2
KEY_A = ("a.2026-10-20.1", 1)
KEY_B = ("b.2026-10-20.1", 1)


def run(body: Callable[[FakeClock, AgentBook], Awaitable[None]]) -> None:
    """Run ``body`` on the loop with a fresh fake clock and book."""
    clock = FakeClock(START)
    book = AgentBook(clock=clock, grace=GRACE)

    async def main() -> None:
        """Run the body, then close the book."""
        try:
            await body(clock, book)
        finally:
            book.close()

    pynet.loop.run(main())


class TestLiveness:
    """Heartbeats keep an agent online; silence past the grace ends it."""

    def test_heartbeat_keeps_online_and_grace_makes_offline(self) -> None:
        """Heartbeats at +0 s and +50 s keep it online until +110 s."""

        async def body(clock: FakeClock, book: AgentBook) -> None:
            """Beat twice, then fall silent."""
            book.heard("a", tags=["x"], max_running=SLOTS)
            await clock.advance(FIFTY)
            book.heard("a", running=[])
            await clock.advance(GRACE - TEN)
            (state,) = book.states()
            assert state.online
            assert state.went_offline is None
            await clock.advance(TEN)
            (state,) = book.states()
            assert not state.online
            assert state.went_offline == START + FIFTY + GRACE
            assert state.last_seen == START + FIFTY

        run(body)

    def test_none_arguments_keep_stored_values(self) -> None:
        """A bare heartbeat leaves tags and capacity alone; running replaces."""

        async def body(_clock: FakeClock, book: AgentBook) -> None:
            """Say hello, reserve, then send heartbeats."""
            book.heard("a", tags=["x"], max_running=SLOTS, running=[KEY_A])
            book.heard("a")
            (state,) = book.states()
            assert state.tags == frozenset({"x"})
            assert state.max_running == SLOTS
            assert state.running == {KEY_A}
            book.heard("a", running=[KEY_B])
            assert book.states()[0].running == {KEY_B}

        run(body)

    def test_returning_agent_is_online_again(self) -> None:
        """A heartbeat after going offline brings it back."""

        async def body(clock: FakeClock, book: AgentBook) -> None:
            """Go offline, then beat."""
            book.heard("a", max_running=1)
            await clock.advance(GRACE)
            assert not book.states()[0].online
            book.heard("a")
            assert book.states()[0].online

        run(body)

    def test_states_are_sorted_copies(self) -> None:
        """States come back sorted by name and cannot mutate the book."""

        async def body(_clock: FakeClock, book: AgentBook) -> None:
            """Add two agents and mutate a returned state."""
            book.heard("b", max_running=1)
            book.heard("a", max_running=1)
            states = book.states()
            assert [s.name for s in states] == ["a", "b"]
            states[0].running.add(KEY_A)
            assert book.states()[0].running == set()

        run(body)


class TestChoose:
    """Placement follows P4-R7."""

    def test_choose_by_name(self) -> None:
        """Online with room is chosen; full is busy; offline or unknown is offline."""

        async def body(clock: FakeClock, book: AgentBook) -> None:
            """Walk one agent through free, busy and offline."""
            none = frozenset[str]()
            assert book.choose(agent="b", tags=none) == Refusal("waiting for agent b (offline)")
            book.heard("b", max_running=1)
            assert book.choose(agent="b", tags=none) == "b"
            book.reserve("b", KEY_A)
            book.reserve("b", KEY_A)
            assert book.choose(agent="b", tags=none) == Refusal("waiting for agent b (busy)")
            book.release("b", KEY_A)
            book.release("b", KEY_A)
            assert book.choose(agent="b", tags=none) == "b"
            await clock.advance(GRACE)
            assert book.choose(agent="b", tags=none) == Refusal("waiting for agent b (offline)")

        run(body)

    def test_zero_capacity_is_busy(self) -> None:
        """An agent that has not said hello has no slots."""

        async def body(_clock: FakeClock, book: AgentBook) -> None:
            """Hear a bare heartbeat."""
            book.heard("a")
            assert book.choose(agent="a", tags=frozenset()) == Refusal("waiting for agent a (busy)")

        run(body)

    def test_choose_by_tags_least_busy_then_name(self) -> None:
        """The less busy tagged agent wins; ties go by name; none gives the tags reason."""

        async def body(_clock: FakeClock, book: AgentBook) -> None:
            """Three agents, two with the tags."""
            tags = frozenset({"linux", "gpu"})
            reason = Refusal("waiting for an agent with tags gpu, linux")
            book.heard("a", tags=["linux"], max_running=SLOTS)
            assert book.choose(agent=None, tags=tags) == reason
            book.heard("c", tags=["gpu", "linux"], max_running=SLOTS)
            book.heard("b", tags=["gpu", "linux", "x"], max_running=SLOTS)
            assert book.choose(agent=None, tags=tags) == "b"
            book.reserve("b", KEY_A)
            assert book.choose(agent=None, tags=tags) == "c"
            book.reserve("c", KEY_B)
            assert book.choose(agent=None, tags=tags) == "b"
            book.reserve("b", KEY_B)
            book.reserve("c", KEY_A)
            assert book.choose(agent=None, tags=tags) == reason

        run(body)


class TestWatch:
    """Watchers hear about real changes only."""

    def test_watch_notifies_on_change(self) -> None:
        """Online flips, load and capacity changes notify; no-ops and unsubscribed do not."""

        async def body(clock: FakeClock, book: AgentBook) -> None:
            """Provoke each kind of change."""
            seen: list[AgentState] = []
            stop = book.watch(seen.append)
            book.heard("a", tags=["x"], max_running=SLOTS)
            count = len(seen)
            assert count >= 1
            book.heard("a")
            book.heard("a", running=[])
            assert len(seen) == count
            book.reserve("a", KEY_A)
            book.reserve("a", KEY_A)
            assert len(seen) == count + 1
            assert seen[-1].running == {KEY_A}
            book.release("a", KEY_A)
            book.release("a", KEY_A)
            assert len(seen) == count + 2
            await clock.advance(GRACE)
            assert not seen[-1].online
            assert len(seen) == count + 3
            book.heard("a")
            assert seen[-1].online
            stop()
            before = len(seen)
            book.reserve("a", KEY_B)
            assert len(seen) == before

        run(body)
