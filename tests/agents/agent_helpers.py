"""Helpers for the agent tests: a pretend scheduler on a real broker, and an agent factory."""

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop
from pymq.broker import BrokerClient, BrokerServer, Delivery
from pynet.messaging import JsonValue

from pyscheduler.agents.journal import Journal
from pyscheduler.agents.messages import (
    QUEUE_CONFIG,
    REPORT_TYPES,
    REPORTS_QUEUE,
    WORK_TYPE,
    WorkOrder,
    message_id,
    work_payload,
    work_queue,
)
from pyscheduler.agents.signing import Message, Rejected, Verifier, seal
from pyscheduler.agents.worker import Agent
from pyscheduler.engine.clock import Clock, FakeClock

NAME = "build-01"
SECRET = "s" * 40
WRONG_SECRET = "w" * 40
HOST = "127.0.0.1"
WAIT = 8.0
POLL = 0.02
JOB = "nightly"
RUN_DATE = "2026-10-20"
RUN_ID = f"{JOB}.{RUN_DATE}.1"
MAX_RUNNING = 4
TAGS = frozenset({"linux", "gpu"})


def order(argv: list[str], *, run_id: str = RUN_ID, attempt: int = 1) -> WorkOrder:
    """Return a work order for ``argv`` with ``JOB`` and ``RUN_DATE``."""
    env = {"PYSCHED_RUN_ID": run_id}
    return WorkOrder(run_id, attempt, JOB, RUN_DATE, tuple(argv), None, env)


def shell(script: str, **kwargs: str | int) -> WorkOrder:
    """Return a work order that runs ``script`` under ``/bin/sh -c``."""
    run_id = str(kwargs.get("run_id", RUN_ID))
    attempt = int(kwargs.get("attempt", 1))
    return order(["/bin/sh", "-c", script], run_id=run_id, attempt=attempt)


@dataclass
class Scheduler:
    """Play the scheduler: send sealed orders to the agent and collect its verified reports."""

    port: int
    clock: FakeClock
    reports: list[Message] = field(default_factory=list[Message])
    client: BrokerClient | None = None

    async def start(self) -> None:
        """Connect, declare both queues and start reading reports."""
        verifier = Verifier(
            lambda n: SECRET if n == NAME else None, clock=self.clock, accept=REPORT_TYPES
        )

        async def heard(delivery: Delivery) -> None:
            """Keep every report that verifies."""
            with contextlib.suppress(Rejected):
                self.reports.append(verifier.check(delivery.body, delivery.headers))

        self.client = BrokerClient(HOST, self.port, initial_backoff=0.05, max_backoff=0.2)
        await self.client.start()
        await self.client.wait_connected(WAIT)
        await self.client.declare_queue(REPORTS_QUEUE, QUEUE_CONFIG)
        await self.client.declare_queue(work_queue(NAME), QUEUE_CONFIG)
        await self.client.consume(REPORTS_QUEUE, heard)

    async def close(self) -> None:
        """Hang up."""
        if self.client is not None:
            await self.client.close()

    async def send(
        self,
        kind: str,
        payload: dict[str, JsonValue],
        *,
        secret: str = SECRET,
        agent: str = NAME,
        at: datetime | None = None,
        msg_id: str | None = None,
    ) -> bool:
        """Seal and put one order on the agent's queue; return whether the broker took it."""
        assert self.client is not None
        body, headers = seal(kind, agent, payload, secret, now=at or self.clock.now())
        put = await self.client.put(work_queue(NAME), body, headers=headers, id=msg_id)
        return not put.duplicate

    async def put_raw(self, body: JsonValue, headers: dict[str, str]) -> None:
        """Put ``body`` and ``headers`` on the agent's queue exactly as given."""
        assert self.client is not None
        await self.client.put(work_queue(NAME), body, headers=headers)

    async def settled(self) -> bool:
        """Return whether every order put on the agent's queue has been acked."""
        assert self.client is not None
        stats = await self.client.stats(work_queue(NAME))
        return stats.in_flight == 0 and stats.ready == 0 and stats.acked == stats.enqueued

    async def work(self, work: WorkOrder, *, fresh_id: bool = False) -> bool:
        """Send ``work`` with its dispatch id (or a new id, to pass the broker's dedupe)."""
        msg_id = None if fresh_id else message_id(work.run_id, work.attempt)
        return await self.send(WORK_TYPE, work_payload(work), msg_id=msg_id)

    def of(self, kind: str) -> list[Message]:
        """Return the reports of ``kind`` heard so far."""
        return [m for m in self.reports if m.kind == kind]

    async def wait_for(self, kind: str, count: int = 1) -> Message:
        """Wait until ``count`` reports of ``kind`` have arrived; return the last."""
        await until(lambda: len(self.of(kind)) >= count)
        return self.of(kind)[count - 1]


async def until(check: Callable[[], bool]) -> None:
    """Poll on the loop until ``check()`` is true (``TimeoutError`` after ``WAIT``)."""
    async with pynet.loop.timeout(WAIT):
        while not check():
            await pynet.loop.sleep(POLL)


async def restart_broker(server: BrokerServer) -> BrokerServer:
    """Close ``server`` and start a fresh, empty one on the same port."""
    port = server.port
    await server.close()
    fresh = BrokerServer()
    await fresh.start(HOST, port)
    return fresh


def make_agent(tmp_path: Path, port: int, clock: Clock) -> Agent:
    """Return an agent (not started) keeping its state and logs under ``tmp_path``."""
    return Agent(
        name=NAME,
        tags=TAGS,
        secret=SECRET,
        broker=(HOST, port),
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        max_running=MAX_RUNNING,
        clock=clock,
    )


def now_clock() -> FakeClock:
    """Return a fake clock showing the real time now (messages must look fresh)."""
    return FakeClock(datetime.now(UTC))


def journal_of(tmp_path: Path) -> Journal:
    """Open the agent's journal under ``tmp_path`` (while no agent holds it)."""
    return Journal.open(tmp_path / "state", sync=False)


def lines(path: Path) -> list[str]:
    """Return the lines of ``path``, or none if it is missing."""
    return path.read_text().splitlines() if path.exists() else []


@dataclass
class World:
    """A broker, a pretend scheduler and an agent sharing one fake clock."""

    tmp_path: Path
    server: BrokerServer
    clock: FakeClock
    scheduler: Scheduler
    agent: Agent

    async def restart_broker(self) -> None:
        """Swap the broker for a fresh, empty one on the same port."""
        self.server = await restart_broker(self.server)

    async def restart_agent(self) -> None:
        """Stop the agent and start a new one on the same state and log folders."""
        await self.agent.stop()
        self.agent = make_agent(self.tmp_path, self.server.port, self.clock)
        await self.agent.start()


def run_world(tmp_path: Path, body: Callable[[World], Awaitable[None]]) -> None:
    """Start a broker, a scheduler and an agent, run ``body``, then close everything."""

    async def main() -> None:
        """Set up, run the test body and always tidy up."""
        server = BrokerServer()
        await server.start(HOST, 0)
        clock = now_clock()
        scheduler = Scheduler(server.port, clock)
        await scheduler.start()
        agent = make_agent(tmp_path, server.port, clock)
        world = World(tmp_path, server, clock, scheduler, agent)
        try:
            await agent.start()
            await body(world)
        finally:
            await world.agent.stop()
            await scheduler.close()
            await world.server.close()

    pynet.loop.run(main())
