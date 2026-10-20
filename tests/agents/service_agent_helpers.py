"""Helpers for the service tests with agents: a real ``serve`` and a real agent on one loop."""

import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pynet.loop
from agent_helpers import HOST, NAME, SECRET, TAGS, until

from pyscheduler.agents.worker import Agent
from pyscheduler.client import ApiClient
from pyscheduler.engine.clock import SystemClock
from pyscheduler.service import serve
from service_helpers import POLL, READY_SECONDS, write_config

SECRETS_MODE = 0o600
AGENT_SLOTS = 2

REMOTE_JOBS = f"""
jobs:
  remote:
    command: "echo hello from the agent"
    schedule: manual
    agent: {NAME}
  slow:
    command: "sleep 8"
    schedule: manual
    agent_tags: [linux]
"""
"""``remote`` runs on the agent by name; ``slow`` by tag, and outlasts a prompt stop of ``serve``."""


def free_port() -> int:
    """Return a TCP port on loopback that nothing listens on right now."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        port: int = probe.getsockname()[1]
    return port


def write_secrets(folder: Path, secret: str = SECRET) -> None:
    """Write ``agents.yaml`` (owner-only) naming ``NAME`` with ``secret``."""
    path = folder / "agents.yaml"
    path.write_text(f"- {{name: {NAME}, secret: {secret}}}\n", encoding="utf-8")
    path.chmod(SECRETS_MODE)


def write_agents_config(folder: Path, broker_port: int, *, secret: str = SECRET) -> Path:
    """Write the secrets and a config whose embedded broker listens on ``broker_port``."""
    write_secrets(folder, secret)
    section = f'agents: {{listen: "{HOST}:{broker_port}", secrets_file: agents.yaml}}\n'
    return write_config(folder, section + REMOTE_JOBS.lstrip("\n"))


@dataclass
class Running:
    """What a test body gets: the API's URL, a client, the broker port and the agent."""

    url: str
    client: ApiClient
    broker_port: int
    agent: Agent
    serve_stopped_at: float | None = None
    """When ``serve`` returned (``time.monotonic``), set by ``run_service``."""

    async def call[T](self, work: Callable[[ApiClient], T]) -> T:
        """Run ``work(client)`` on a helper thread (the client blocks)."""
        return await pynet.loop.run_in_thread(lambda: work(self.client))

    async def agent_online(self) -> None:
        """Wait until ``/api/agents`` lists the agent online."""

        async def online() -> bool:
            """Return whether the API lists the agent online."""
            agents = (await self.call(lambda c: c.agents()))["agents"]
            return any(a["name"] == NAME and a["online"] for a in agents)

        async with pynet.loop.timeout(READY_SECONDS):
            while not await online():
                await pynet.loop.sleep(POLL)


def make_service_agent(folder: Path, broker_port: int) -> Agent:
    """Return a real agent (not started) for the service's embedded broker."""
    return Agent(
        name=NAME,
        tags=TAGS,
        secret=SECRET,
        broker=(HOST, broker_port),
        state_dir=folder / "agent-state",
        log_dir=folder / "agent-logs",
        max_running=AGENT_SLOTS,
        clock=SystemClock(),
    )


def run_service[T](
    folder: Path,
    body: Callable[[Running], Awaitable[T]],
    *,
    secret: str = SECRET,
    start_agent: bool = True,
    stop_serve_first: bool = False,
) -> T:
    """Run ``serve`` with an embedded broker and (unless told not to) a real agent; run ``body``.

    The agent stops first, then ``serve`` is stopped through its ``stop``
    event; with ``stop_serve_first`` it is the other way round, so ``serve``
    stops while the agent still runs its work.
    """
    broker_port = free_port()
    path = write_agents_config(folder, broker_port, secret=secret)

    async def main() -> T:
        """Start serve and the agent, run the body, then stop both."""
        stop = pynet.loop.Event()
        ports: list[int] = []
        task = pynet.loop.spawn(serve(path, ready=ports.append, stop=stop), name="serve")
        await until(lambda: bool(ports) or task.done())
        if task.done():
            task.result()
            msg = "serve ended before it was ready"
            raise RuntimeError(msg)
        url = f"http://127.0.0.1:{ports[0]}"
        agent = make_service_agent(folder, broker_port)
        running = Running(url, ApiClient(url, None), broker_port, agent)

        async def stop_serve() -> None:
            """Stop ``serve`` and note when it returned."""
            stop.set()
            await task
            running.serve_stopped_at = time.monotonic()

        try:
            if start_agent:
                await agent.start()
            return await body(running)
        finally:
            try:
                if stop_serve_first:
                    await stop_serve()
            finally:
                try:
                    if start_agent:
                        await agent.stop()
                finally:
                    if not stop_serve_first:
                        await stop_serve()

    return pynet.loop.run(main())
