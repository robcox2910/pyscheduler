"""Helpers for the hub tests: a scripted fake agent on a real broker, and a hub factory."""

import textwrap
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pynet.loop
from agent_helpers import HOST, NAME, RUN_ID, SECRET, TAGS, WAIT, now_clock, order, until
from pymq.broker import BrokerClient, BrokerServer, Delivery
from pynet.messaging import JsonValue

from pyscheduler.agents.hub import AgentHub
from pyscheduler.agents.messages import (
    FINISHED_TYPE,
    HEARTBEAT_TYPE,
    HELLO_TYPE,
    QUEUE_CONFIG,
    REPORTS_QUEUE,
    SCHEDULER_TYPES,
    STARTED_TYPE,
    WORK_TYPE,
    RunKey,
    finished_payload,
    heartbeat_payload,
    hello_payload,
    started_payload,
    work_payload,
    work_queue,
)
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.agents.signing import Message, Rejected, Verifier, seal
from pyscheduler.config import Config, load_config
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.status_note import AttemptResult

KEY: RunKey = (RUN_ID, 1)
OUT = "/agent/logs/nightly/out"
ERR = "/agent/logs/nightly/err"
SECRETS_MODE = 0o600
FAKE_SLOTS = 4
"""How many attempts the fake agent says it has room for."""


def hub_config(tmp_path: Path, where: str) -> Config:
    """Load a config whose ``agents:`` section has ``where`` (``listen: ...`` or ``broker: ...``)."""
    secrets = tmp_path / "agents.yaml"
    secrets.write_text(f"- {{name: {NAME}, secret: {SECRET}}}\n", encoding="utf-8")
    secrets.chmod(SECRETS_MODE)
    path = tmp_path / "scheduler.yaml"
    text = f"""\
        agents: {{{where}, secrets_file: agents.yaml}}
        jobs:
          nightly:
            command: "true"
            schedule: manual
        """
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_config(path, env={})


def work(attempt: int = 1) -> dict[str, JsonValue]:
    """Return the work payload for attempt ``attempt`` of ``RUN_ID``."""
    return work_payload(order(["true"], attempt=attempt))


@dataclass
class FakeAgent:
    """Play an agent: read sealed orders from its queue and post scripted reports."""

    port: int
    clock: FakeClock
    orders: list[tuple[str, Message]] = field(default_factory=list[tuple[str, Message]])
    refused: list[str] = field(default_factory=list[str])
    client: BrokerClient | None = None

    async def start(self) -> None:
        """Connect, declare both queues and keep every verified order with its delivery id."""
        verifier = Verifier(
            lambda n: SECRET if n == NAME else None, clock=self.clock, accept=SCHEDULER_TYPES
        )

        async def got(delivery: Delivery) -> None:
            """Keep the order if it verifies, else note why it was refused."""
            try:
                self.orders.append((delivery.id, verifier.check(delivery.body, delivery.headers)))
            except Rejected as exc:
                self.refused.append(exc.reason)

        self.client = BrokerClient(HOST, self.port, initial_backoff=0.05, max_backoff=0.2)
        await self.client.start()
        await self.client.wait_connected(WAIT)
        await self.client.declare_queue(REPORTS_QUEUE, QUEUE_CONFIG)
        await self.client.declare_queue(work_queue(NAME), QUEUE_CONFIG)
        await self.client.consume(work_queue(NAME), got)

    async def close(self) -> None:
        """Hang up."""
        if self.client is not None:
            await self.client.close()

    async def report(
        self,
        kind: str,
        payload: dict[str, JsonValue],
        *,
        secret: str = SECRET,
        agent: str = NAME,
    ) -> str:
        """Seal ``payload`` as a report of ``kind``, post it and return its nonce."""
        assert self.client is not None
        body, headers = seal(kind, agent, payload, secret, now=self.clock.now())
        await self.client.put(REPORTS_QUEUE, body, headers=headers)
        return str(body["nonce"])

    async def hello(self, running: Iterable[RunKey] = ()) -> None:
        """Say hello with ``TAGS``, room for four and ``running``."""
        await self.report(HELLO_TYPE, hello_payload(TAGS, FAKE_SLOTS, running))

    async def heartbeat(self, running: Iterable[RunKey] = (), *, capacity: bool = False) -> str:
        """Send a heartbeat listing ``running`` (with ``TAGS`` and room for four if ``capacity``).

        Returns:
            The heartbeat's nonce.

        """
        if capacity:
            payload = heartbeat_payload(running, tags=TAGS, max_running=FAKE_SLOTS)
        else:
            payload = heartbeat_payload(running)
        return await self.report(HEARTBEAT_TYPE, payload)

    async def started(self, key: RunKey = KEY) -> None:
        """Report ``key`` started with the ``OUT``/``ERR`` log paths."""
        await self.report(STARTED_TYPE, started_payload(key[0], key[1], OUT, ERR))

    async def finished(self, key: RunKey = KEY, exit_code: int = 0, **kwargs: str) -> str:
        """Report ``key`` finished with ``exit_code``; return the report's nonce."""
        result = AttemptResult(exit_code, None, self.clock.now(), self.clock.now(), None)
        return await self.report(FINISHED_TYPE, finished_payload(key[0], key[1], result), **kwargs)

    def of(self, kind: str) -> list[tuple[str, Message]]:
        """Return the ``(delivery id, order)`` pairs of ``kind`` received so far."""
        return [(i, m) for i, m in self.orders if m.kind == kind]

    async def wait_for(self, kind: str, count: int = 1) -> tuple[str, Message]:
        """Wait until ``count`` orders of ``kind`` have arrived; return the last."""
        await until(lambda: len(self.of(kind)) >= count)
        return self.of(kind)[count - 1]

    async def works(self, count: int) -> list[str]:
        """Wait for ``count`` work orders and return their delivery ids."""
        await self.wait_for(WORK_TYPE, count)
        return [i for i, _ in self.of(WORK_TYPE)]


@dataclass
class HubWorld:
    """A hub, a fake agent sharing its fake clock, and (in broker mode) the outside broker."""

    hub: AgentHub
    agent: FakeAgent
    clock: FakeClock
    server: BrokerServer | None = None

    async def online(self) -> None:
        """Say hello and wait until the book has the agent online."""
        await self.agent.hello()
        await until(lambda: any(s.online for s in self.hub.book.states()))

    async def restart_broker(self) -> None:
        """Swap the outside broker for a fresh, empty one on the same port."""
        assert self.server is not None
        port = self.server.port
        await self.server.close()
        self.server = BrokerServer()
        await self.server.start(HOST, port)


async def started_handle(w: HubWorld) -> RemoteHandle:
    """Bring the agent online, dispatch attempt 1 and have it report started."""
    await w.online()
    handle = w.hub.dispatch(NAME, work())
    await w.agent.wait_for(WORK_TYPE)
    await w.agent.started()
    await until(lambda: handle.stdout_path is not None)
    return handle


def run_hub(
    tmp_path: Path, body: Callable[[HubWorld], Awaitable[None]], *, outside: bool = False
) -> None:
    """Start a hub (embedded broker, or an ``outside`` one) and a fake agent, run ``body``."""

    async def main() -> None:
        """Set up, run the test body and always tidy up."""
        clock = now_clock()
        server = None
        if outside:
            server = BrokerServer()
            await server.start(HOST, 0)
            config = hub_config(tmp_path, f'broker: "{HOST}:{server.port}"')
        else:
            config = hub_config(tmp_path, f'listen: "{HOST}:0"')
        assert config.agents is not None
        hub = AgentHub(config, clock=clock, secrets=config.agents.secrets)
        await hub.start()
        await until(lambda: hub.attached)
        agent = FakeAgent(hub.address[1], clock)
        world = HubWorld(hub, agent, clock, server)
        try:
            await agent.start()
            await body(world)
        finally:
            await world.agent.close()
            await hub.stop()
            if world.server is not None:
                await world.server.close()

    pynet.loop.run(main())


def secrets_with(secret: str) -> AgentSecrets:
    """Return secrets naming only ``NAME`` with ``secret``."""
    return AgentSecrets({NAME: secret})
