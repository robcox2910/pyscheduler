"""Hub tests for life after a restart: heartbeats that bring capacity, and orphaned work."""

from datetime import timedelta
from pathlib import Path

import pynet.loop
from agent_helpers import NAME, TAGS, until
from hub_helpers import FAKE_SLOTS, KEY, HubWorld, run_hub, started_handle

from pyscheduler.agents.messages import KILL_TYPE, MAX_SKEW, kill_payload

GRACE = timedelta(seconds=60)
LOST_AFTER = timedelta(minutes=10)
KILL_GRACE = timedelta(seconds=10)
"""The default ``kill_grace``: how long a re-sent ``TERM`` has before ``KILL`` follows."""
AWAY = MAX_SKEW + timedelta(minutes=1)
"""Longer than the allowed clock skew, so the ``TERM`` sent at lose time would be stale."""
SETTLE = 0.2
FIRST, SECOND, THIRD, FOURTH = 1, 2, 3, 4


async def _settle() -> None:
    """Give in-flight messages a moment of real time to land."""
    await pynet.loop.sleep(SETTLE)


async def _lost(w: HubWorld) -> None:
    """Start ``KEY`` on the agent, let recovery finish, go silent until it is lost."""
    handle = await started_handle(w)
    w.hub.recovered()
    await w.clock.advance(GRACE)
    await w.clock.advance(LOST_AFTER)
    await handle.wait()
    assert handle.lost
    await w.agent.wait_for(KILL_TYPE)  # the one ``TERM`` sent at lose time


def _signals(w: HubWorld) -> list[str]:
    """Return the signals of every ``kill`` the agent received, in order."""
    return [str(m.payload["signal"]) for _, m in w.agent.of(KILL_TYPE)]


class TestHeartbeatCapacity:
    """A heartbeat alone (no hello) tells a fresh hub the agent's tags and room."""

    def test_heartbeat_without_hello_gives_capacity(self, tmp_path: Path) -> None:
        """Place work on an agent this hub has only heard heartbeats from."""

        async def body(w: HubWorld) -> None:
            """Beat with capacity, never say hello, then choose by name and by tag."""
            await w.agent.heartbeat(capacity=True)
            await until(lambda: any(s.online for s in w.hub.book.states()))
            [state] = w.hub.book.states()
            assert (state.tags, state.max_running) == (TAGS, FAKE_SLOTS)
            assert w.hub.book.choose(agent=NAME, tags=frozenset()) == NAME
            assert w.hub.book.choose(agent=None, tags=TAGS) == NAME

        run_hub(tmp_path, body)

    def test_old_heartbeat_keeps_capacity(self, tmp_path: Path) -> None:
        """Keep the hello's tags and room when a heartbeat does not carry them."""

        async def body(w: HubWorld) -> None:
            """Say hello, then send an old-shape heartbeat."""
            await w.online()
            await w.agent.heartbeat([KEY])
            await until(lambda: w.hub.book.states()[0].running == {KEY})
            [state] = w.hub.book.states()
            assert (state.tags, state.max_running) == (TAGS, FAKE_SLOTS)

        run_hub(tmp_path, body)


class TestOrphans:
    """Work the hub gave up on is stopped again when its agent reports it still running."""

    def test_lost_attempt_reported_later_is_killed(self, tmp_path: Path) -> None:
        """Send a fresh ``TERM``, then ``KILL`` after the grace, without spamming either."""

        async def body(w: HubWorld) -> None:
            """Lose ``KEY``, come back after more than ``MAX_SKEW`` still running it."""
            await _lost(w)
            await w.clock.advance(AWAY)
            back = w.clock.now()
            await w.agent.hello([KEY])
            _, term = await w.agent.wait_for(KILL_TYPE, SECOND)
            assert term.payload == kill_payload(*KEY, "TERM")
            assert term.sent_at >= back  # sealed afresh, so the agent accepts it
            await w.agent.heartbeat([KEY], capacity=True)
            await _settle()
            assert len(w.agent.of(KILL_TYPE)) == SECOND  # no second TERM within the grace
            await w.clock.advance(KILL_GRACE)
            _, kill = await w.agent.wait_for(KILL_TYPE, THIRD)
            assert kill.payload == kill_payload(*KEY, "KILL")
            await w.agent.heartbeat([KEY], capacity=True)
            await _settle()
            assert len(w.agent.of(KILL_TYPE)) == THIRD
            await w.clock.advance(KILL_GRACE)
            await w.agent.heartbeat([KEY], capacity=True)
            await w.agent.wait_for(KILL_TYPE, FOURTH)
            assert _signals(w) == ["TERM", "TERM", "KILL", "TERM"]
            assert w.agent.refused == []

        run_hub(tmp_path, body)

    def test_orphan_that_ends_gets_no_kill(self, tmp_path: Path) -> None:
        """Send no ``KILL`` for an orphan the agent stops listing within the grace."""

        async def body(w: HubWorld) -> None:
            """Report the lost key once, then a heartbeat without it."""
            await _lost(w)
            await w.agent.hello([KEY])
            await w.agent.wait_for(KILL_TYPE, SECOND)
            await w.agent.heartbeat([], capacity=True)
            await until(lambda: w.hub.book.states()[0].running == set())
            await w.clock.advance(KILL_GRACE)
            await _settle()
            assert _signals(w) == ["TERM", "TERM"]

        run_hub(tmp_path, body)

    def test_nothing_is_killed_before_recovery(self, tmp_path: Path) -> None:
        """Leave unknown running work alone until recovery has adopted what it will."""

        async def body(w: HubWorld) -> None:
            """Report an attempt the hub never dispatched, before ``recovered()``."""
            await w.agent.hello([KEY])
            await until(lambda: any(s.running == {KEY} for s in w.hub.book.states()))
            await w.clock.advance(KILL_GRACE)
            await _settle()
            assert w.agent.of(KILL_TYPE) == []

        run_hub(tmp_path, body)
