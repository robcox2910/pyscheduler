"""Tests for the hub's harder cases: unknown reports, rival agents, races and a missing broker."""

import socket
from pathlib import Path

import pynet.loop
import pytest
from agent_helpers import HOST, NAME, RUN_ID, SECRET, WAIT, now_clock, until
from hub_helpers import KEY, FakeAgent, HubWorld, hub_config, run_hub, started_handle, work
from pymq.broker import BrokerServer

from pyscheduler.agents.hub import LOG_TIMEOUT, AgentHub
from pyscheduler.agents.messages import ACK_TYPE, QUEUE_CONFIG, WORK_TYPE, key_payload, work_queue
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.engine.status_note import AttemptResult

RIVAL = "rival-02"
RIVAL_SECRET = "r" * 40
SETTLE = 0.2
SECOND = 2
UNKNOWN = (RUN_ID, 9)


class TestReports:
    """Reports the hub cannot match to an attempt it follows."""

    def test_unknown_finished_acked_after_recovered(self, tmp_path: Path) -> None:
        """Leave an unknown ``finished`` unacked before ``recovered()``; ack and drop it after."""

        async def body(w: HubWorld) -> None:
            """Report an unknown attempt finished, before and after recovery."""
            await w.online()
            nonce = await w.agent.finished(UNKNOWN)
            await until(lambda: nonce in w.hub._verifier._seen)
            await pynet.loop.sleep(SETTLE)
            assert w.agent.of(ACK_TYPE) == []
            w.hub.recovered()
            await w.agent.finished(UNKNOWN)
            _, ack = await w.agent.wait_for(ACK_TYPE)
            assert ack.payload == key_payload(*UNKNOWN)

        run_hub(tmp_path, body)

    @pytest.mark.parametrize("recovered", [False, True])
    def test_rival_agent_cannot_settle_or_be_acked(self, tmp_path: Path, recovered: bool) -> None:
        """Ignore another known agent's ``finished`` for this agent's attempt, even after recovery."""

        async def body(w: HubWorld) -> None:
            """Let a rival with its own valid secret claim the attempt."""
            w.hub.set_secrets(AgentSecrets({NAME: SECRET, RIVAL: RIVAL_SECRET}))
            handle = await started_handle(w)
            if recovered:
                w.hub.recovered()
            nonce = await w.agent.finished(secret=RIVAL_SECRET, agent=RIVAL)
            await until(lambda: nonce in w.hub._verifier._seen)
            await pynet.loop.sleep(SETTLE)
            assert not handle.done
            client = w.agent.client
            assert client is not None
            await client.declare_queue(work_queue(RIVAL), QUEUE_CONFIG)
            assert (await client.stats(work_queue(RIVAL))).enqueued == 0

        run_hub(tmp_path, body)

    def test_finished_and_lost_together_settle_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settle once when ``finished`` and the lost countdown land at the same instant.

        Both orders are driven directly, as the loop would run them back to back.
        """

        async def body(w: HubWorld) -> None:
            """Finish-then-lose one attempt and lose-then-finish another."""
            first = await started_handle(w)
            second = w.hub.dispatch(NAME, work(attempt=SECOND))
            settled: list[bool] = []
            real = w.hub._settle

            def counting(handle: RemoteHandle, result: AttemptResult, *, lost: bool) -> None:
                """Count each settling, then settle."""
                settled.append(lost)
                real(handle, result, lost=lost)

            monkeypatch.setattr(w.hub, "_settle", counting)
            now = w.clock.now()
            done = AttemptResult(0, None, now, now, None)
            w.hub._finished(NAME, KEY, done)
            w.hub._lose(KEY)
            w.hub._lose(second.key)
            w.hub._finished(NAME, second.key, done)
            assert settled == [False, True]
            assert await first.wait() == done
            assert not first.lost
            assert second.lost
            await w.agent.wait_for(ACK_TYPE, SECOND)  # both reports are acked

        run_hub(tmp_path, body)


class TestBrokerDown:
    """The hub starts, and stays bounded, while its broker cannot be reached."""

    def test_start_returns_while_broker_down(self, tmp_path: Path) -> None:
        """Return from ``start()`` at once, then attach and dispatch once the broker is up."""
        port = _free_port()

        async def main() -> None:
            """Start with no broker, then bring one up on the port."""
            clock = now_clock()
            config = hub_config(tmp_path, f'broker: "{HOST}:{port}"')
            assert config.agents is not None
            hub = AgentHub(config, clock=clock, secrets=config.agents.secrets)
            async with pynet.loop.timeout(WAIT):
                await hub.start()
            assert not hub.attached
            server = BrokerServer()
            await server.start(HOST, port)
            agent = FakeAgent(port, clock)
            try:
                await agent.start()
                await until(lambda: hub.attached)
                await agent.hello()
                await until(lambda: any(s.online for s in hub.book.states()))
                hub.dispatch(NAME, work())
                await agent.wait_for(WORK_TYPE)
            finally:
                await agent.close()
                await hub.stop()
                await server.close()

        pynet.loop.run(main())

    def test_stop_while_broker_down(self, tmp_path: Path) -> None:
        """Stop cleanly while still trying to attach."""

        async def main() -> None:
            """Start against a closed port and stop."""
            config = hub_config(tmp_path, f'broker: "{HOST}:{_free_port()}"')
            assert config.agents is not None
            hub = AgentHub(config, clock=now_clock(), secrets=config.agents.secrets)
            await hub.start()
            await hub.stop()
            assert hub._tasks == set()
            assert not hub.attached

        pynet.loop.run(main())

    def test_fetch_log_times_out_with_broker_down(self, tmp_path: Path) -> None:
        """Raise ``TimeoutError`` after ``LOG_TIMEOUT`` even if the send cannot get through."""

        async def body(w: HubWorld) -> None:
            """Close the broker, ask for a log, and walk the clock past the timeout."""
            await w.online()
            assert w.server is not None
            port = w.server.port
            await w.server.close()
            ask = pynet.loop.spawn(w.hub.fetch_log(NAME, RUN_ID, 1, "stdout", mode="tail", count=1))
            await pynet.loop.sleep(SETTLE)
            await w.clock.advance(LOG_TIMEOUT)
            with pytest.raises(TimeoutError):
                await ask
            w.server = BrokerServer()
            await w.server.start(HOST, port)

        run_hub(tmp_path, body, outside=True)


def _free_port() -> int:
    """Return a loopback port that nothing listens on (just freed)."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])
