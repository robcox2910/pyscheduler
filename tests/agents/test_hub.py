"""Tests for the scheduler's agent hub: real broker, scripted fake agent, fake clock."""

import logging
from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from agent_helpers import NAME, RUN_ID, SECRET, WRONG_SECRET, until
from hub_helpers import ERR, KEY, OUT, HubWorld, run_hub, started_handle, work

from pyscheduler.agents.hub import LOG_TIMEOUT, AgentOffline
from pyscheduler.agents.log_messages import LogReply, log_reply_payload
from pyscheduler.agents.messages import (
    ACK_TYPE,
    KILL_TYPE,
    LOG_REPLY_TYPE,
    LOG_REQUEST_TYPE,
    WORK_TYPE,
    key_payload,
    message_id,
)
from pyscheduler.agents.remote_handle import RemoteHandle

GRACE = timedelta(seconds=60)
LOST_AFTER = timedelta(minutes=10)
JUST_BEFORE = timedelta(seconds=1)
SETTLE = 0.2
TAIL_COUNT = 5
FIRST, SECOND, THIRD = 1, 2, 3
OTHER = "other-02"


async def _settle() -> None:
    """Give in-flight messages a moment of real time to land."""
    await pynet.loop.sleep(SETTLE)


class TestDispatch:
    """Work goes out sealed with its dispatch id, and reports resolve the handle."""

    def test_dispatch_and_finish(self, tmp_path: Path) -> None:
        """Resolve ``wait()`` from ``finished`` with exit 0 and ack it exactly once."""

        async def body(w: HubWorld) -> None:
            """Dispatch, report started then finished, and check the handle and acks."""
            seen: list[RemoteHandle] = []
            w.hub.watch_started(seen.append)
            await w.online()
            w.hub.book.reserve(NAME, KEY)
            handle = w.hub.dispatch(NAME, work())
            delivery_id, order = await w.agent.wait_for(WORK_TYPE)
            assert delivery_id == message_id(*KEY)
            assert order.agent == NAME
            assert order.payload == work()
            assert handle.pid is None
            await w.agent.started()
            await until(lambda: seen == [handle])
            assert (handle.stdout_path, handle.stderr_path) == (OUT, ERR)
            await w.agent.finished()
            result = await handle.wait()
            assert result.exit_code == 0
            assert not handle.lost
            _, ack = await w.agent.wait_for(ACK_TYPE)
            assert ack.payload == key_payload(*KEY)
            await _settle()
            assert len(w.agent.of(ACK_TYPE)) == FIRST
            assert w.agent.refused == []
            assert w.hub.book.states()[0].running == set()

        run_hub(tmp_path, body)

    def test_duplicate_finished_is_acked_again(self, tmp_path: Path) -> None:
        """Ack a repeated ``finished`` again without touching the result."""

        async def body(w: HubWorld) -> None:
            """Finish twice, the second time with another exit code."""
            handle = await started_handle(w)
            await w.agent.finished()
            await handle.wait()
            await w.agent.finished(exit_code=THIRD)
            await w.agent.wait_for(ACK_TYPE, SECOND)
            assert (await handle.wait()).exit_code == 0

        run_hub(tmp_path, body)

    def test_unknown_reports_are_ignored(self, tmp_path: Path) -> None:
        """Leave a ``finished`` for an unknown attempt unacked, and ignore its ``started``."""

        async def body(w: HubWorld) -> None:
            """Report on an attempt nobody dispatched, then check nothing was acked."""
            await w.online()
            await w.agent.started()
            await w.agent.finished()
            await w.agent.heartbeat([KEY])
            await until(lambda: w.hub.book.states()[0].running == {KEY})
            await _settle()
            assert w.agent.of(ACK_TYPE) == []

        run_hub(tmp_path, body)

    def test_hub_rejects_forged_reports(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Never resolve a handle from a forged ``finished``; log the reason only."""

        async def body(w: HubWorld) -> None:
            """Send a badly signed and an unknown agent's finished, then a real heartbeat."""
            handle = await started_handle(w)
            await w.agent.finished(secret=WRONG_SECRET)
            await w.agent.finished(agent=OTHER)
            await w.agent.report(ACK_TYPE, key_payload(*KEY))
            await w.agent.heartbeat([KEY])
            await until(lambda: "unexpected type" in caplog.text)
            await _settle()
            assert not handle.done
            assert w.agent.of(ACK_TYPE) == []

        with caplog.at_level(logging.WARNING, "pyscheduler.agents.hub"):
            run_hub(tmp_path, body)
        assert "bad signature" in caplog.text
        assert "unknown agent" in caplog.text
        assert SECRET not in caplog.text
        assert WRONG_SECRET not in caplog.text

    def test_terminate_and_kill_send_signed_kills(self, tmp_path: Path) -> None:
        """Send ``kill`` with ``TERM`` then ``KILL`` from the sync methods."""

        async def body(w: HubWorld) -> None:
            """Terminate, then kill, and read both orders."""
            handle = await started_handle(w)
            handle.terminate()
            handle.kill()
            await w.agent.wait_for(KILL_TYPE, SECOND)
            signals = [m.payload["signal"] for _, m in w.agent.of(KILL_TYPE)]
            assert sorted(str(s) for s in signals) == ["KILL", "TERM"]

        run_hub(tmp_path, body)

    def test_redispatch_until_started(self, tmp_path: Path) -> None:
        """Re-put the work every ``agent_grace`` with the same id until ``started``.

        The broker forgets ids only when it restarts (its dedupe window is
        five minutes of real time), so it is restarted before each grace.
        """

        async def body(w: HubWorld) -> None:
            """Ignore two puts, start on the third, and see no fourth."""
            await w.online()
            handle = w.hub.dispatch(NAME, work())
            await w.agent.works(FIRST)
            for count in (SECOND, THIRD):
                await w.restart_broker()
                await w.clock.advance(GRACE)
                assert await w.agent.works(count) == [message_id(*KEY)] * count
            await w.agent.started()
            await until(lambda: handle.stdout_path is not None)
            await w.restart_broker()
            await w.clock.advance(GRACE * 2)
            handle.terminate()  # an order put after any re-put would have been
            await w.agent.wait_for(KILL_TYPE)
            assert len(w.agent.of(WORK_TYPE)) == THIRD

        run_hub(tmp_path, body, outside=True)


class TestLiveness:
    """Offline agents' attempts are lost after ``lost_after``, unless they report first."""

    def test_offline_then_lost(self, tmp_path: Path) -> None:
        """Mark the agent offline after the grace, then the handle lost after ``lost_after``."""

        async def body(w: HubWorld) -> None:
            """Go silent and walk the clock past both limits."""
            w.hub.book.reserve(NAME, KEY)
            handle = await started_handle(w)
            await w.clock.advance(GRACE)
            assert not w.hub.book.states()[0].online
            await w.clock.advance(LOST_AFTER - JUST_BEFORE)
            assert not handle.done
            await w.clock.advance(JUST_BEFORE)
            result = await handle.wait()
            assert handle.lost
            assert (result.exit_code, result.signal, result.started_at) == (None, None, None)
            assert result.start_error is None
            assert result.ended_at == w.clock.now()
            assert w.hub.book.states()[0].running == set()
            await w.agent.finished()  # too late: acked, and ignored
            await w.agent.wait_for(ACK_TYPE)
            assert handle.lost

        run_hub(tmp_path, body)

    def test_reconnect_before_lost_cancels(self, tmp_path: Path) -> None:
        """Resolve normally from a ``finished`` that comes after offline but before lost."""

        async def body(w: HubWorld) -> None:
            """Go offline, report finished halfway to lost, and walk past lost."""
            handle = await started_handle(w)
            await w.clock.advance(GRACE + LOST_AFTER / 2)
            await w.agent.finished()
            result = await handle.wait()
            await w.clock.advance(LOST_AFTER)
            assert result.exit_code == 0
            assert not handle.lost

        run_hub(tmp_path, body)

    def test_heartbeat_listing_the_attempt_cancels(self, tmp_path: Path) -> None:
        """Keep the handle when the agent comes back and still lists the attempt."""

        async def body(w: HubWorld) -> None:
            """Go offline, heartbeat with the key, and walk past lost."""
            handle = await started_handle(w)
            await w.clock.advance(GRACE)
            await w.agent.heartbeat([KEY])
            await until(lambda: w.hub.book.states()[0].online)
            await w.clock.advance(LOST_AFTER)
            assert not handle.done

        run_hub(tmp_path, body)

    def test_never_started_is_lost(self, tmp_path: Path) -> None:
        """Lose work whose ``started`` never comes within ``lost_after`` (P4-R4)."""

        async def body(w: HubWorld) -> None:
            """Dispatch to a silent agent and walk past ``lost_after``."""
            handle = w.hub.dispatch(NAME, work())
            await w.agent.works(FIRST)
            await w.clock.advance(LOST_AFTER)
            await handle.wait()
            assert handle.lost

        run_hub(tmp_path, body)

    def test_adopt(self, tmp_path: Path) -> None:
        """Arm the lost timer at adopt time for an offline agent; a report resolves it."""

        async def body(w: HubWorld) -> None:
            """Adopt two attempts: one finishes, one is lost."""
            began = w.clock.now()
            done = w.hub.adopt(NAME, RUN_ID, FIRST, began)
            lost = w.hub.adopt(NAME, RUN_ID, SECOND, began)
            assert done.started_at == began
            assert w.hub.book.states()[0].running == {KEY, (RUN_ID, SECOND)}
            await w.agent.finished()
            assert (await done.wait()).exit_code == 0
            await w.agent.wait_for(ACK_TYPE)
            await w.clock.advance(LOST_AFTER)
            await lost.wait()
            assert lost.lost
            assert not done.lost

        run_hub(tmp_path, body)

    def test_adopt_while_online_waits(self, tmp_path: Path) -> None:
        """Arm no lost timer when the agent is online at adopt time."""

        async def body(w: HubWorld) -> None:
            """Adopt with the agent online and keep it online past ``lost_after``."""
            await w.online()
            handle = w.hub.adopt(NAME, RUN_ID, FIRST, w.clock.now())
            for _ in range(int(LOST_AFTER / GRACE) + 1):
                await w.agent.heartbeat([KEY])
                await _settle()
                await w.clock.advance(GRACE - JUST_BEFORE)
            assert not handle.done

        run_hub(tmp_path, body)


class TestLogs:
    """Log requests are answered, time out, or fail fast for an offline agent."""

    def test_fetch_log_reply(self, tmp_path: Path) -> None:
        """Return the agent's reply to a signed ``log_request``."""

        async def body(w: HubWorld) -> None:
            """Ask for a tail and answer it as the agent."""
            await w.online()
            ask = pynet.loop.spawn(
                w.hub.fetch_log(NAME, RUN_ID, FIRST, "stdout", mode="tail", count=TAIL_COUNT)
            )
            _, request = await w.agent.wait_for(LOG_REQUEST_TYPE)
            assert request.payload["count"] == TAIL_COUNT
            request_id = str(request.payload["request_id"])
            reply = LogReply(request_id, lines=["a", "b"])
            await w.agent.report(LOG_REPLY_TYPE, log_reply_payload(reply))
            assert await ask == reply

        run_hub(tmp_path, body)

    def test_fetch_log_timeout_and_offline(self, tmp_path: Path) -> None:
        """Raise ``AgentOffline`` at once or when it drops, ``TimeoutError`` after 10 s."""

        async def body(w: HubWorld) -> None:
            """Ask an unknown agent, then a silent one, then one that goes offline."""
            with pytest.raises(AgentOffline):
                await w.hub.fetch_log(NAME, RUN_ID, FIRST, "stdout", mode="tail", count=1)
            await w.online()
            ask = pynet.loop.spawn(
                w.hub.fetch_log(NAME, RUN_ID, FIRST, "stderr", mode="read", offset=0, limit=9)
            )
            await w.agent.wait_for(LOG_REQUEST_TYPE)
            await w.clock.advance(LOG_TIMEOUT)
            with pytest.raises(TimeoutError):
                await ask
            await w.agent.heartbeat()
            await _settle()
            await w.clock.advance(GRACE - LOG_TIMEOUT / 2)
            ask = pynet.loop.spawn(
                w.hub.fetch_log(NAME, RUN_ID, FIRST, "stdout", mode="tail", count=1)
            )
            await w.agent.wait_for(LOG_REQUEST_TYPE, SECOND)
            await w.clock.advance(LOG_TIMEOUT / 2)  # offline before the reply timeout is up
            with pytest.raises(AgentOffline):
                await ask

        run_hub(tmp_path, body)
