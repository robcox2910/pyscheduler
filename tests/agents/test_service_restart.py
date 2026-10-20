"""Restart ``serve`` with a real agent attached: outside broker and embedded broker."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pynet.loop
import pytest
from agent_helpers import HOST, NAME, now_clock, until
from pymq.broker import BrokerServer
from service_agent_helpers import (
    Serving,
    free_port,
    make_service_agent,
    start_serve,
    write_agents_config,
)

from pyscheduler.agents.messages import HEARTBEAT_EVERY
from pyscheduler.agents.worker import Agent

RESTART_TEST_SECONDS = 60
"""The per-test limit for these tests: each starts ``serve`` twice and runs real jobs."""
LONG_WAIT = 30.0
"""How long a step may take on a loaded machine (real seconds)."""
NAP = 0.05
JOB_SECONDS = 4
"""How long the remote job runs, so ``serve`` can restart while it does."""

PLACED_JOBS = f"""
jobs:
  pinned:
    command: "true"
    schedule: manual
    agent: {NAME}
  tagged:
    command: "true"
    schedule: manual
    agent_tags: [linux]
"""
"""One job placed on the agent by name, one by tag."""


def _today() -> str:
    """Return today's date in UTC (the run date of a triggered run)."""
    return datetime.now(UTC).date().isoformat()


async def _eventually(check: Callable[[], Awaitable[bool]]) -> None:
    """Wait (up to ``LONG_WAIT``) until the async ``check()`` is true."""
    async with pynet.loop.timeout(LONG_WAIT):
        while not await check():
            await pynet.loop.sleep(NAP)


async def _online(s: Serving) -> None:
    """Wait until ``s`` lists the agent online."""

    async def online() -> bool:
        """Return whether the API lists the agent online."""
        agents = (await s.call(lambda c: c.agents()))["agents"]
        return any(a["name"] == NAME and a["online"] for a in agents)

    await _eventually(online)


async def _ends(s: Serving, run_id: str, wanted: str) -> dict[str, Any]:
    """Wait until run ``run_id`` is in state ``wanted``; return its detail."""
    seen: list[dict[str, Any]] = []

    async def there() -> bool:
        """Fetch the run and say whether it reached ``wanted``."""
        detail = await s.call(lambda c: c.run(run_id))
        seen[:] = [detail]
        return detail["run"]["state"] == wanted

    await _eventually(there)
    return seen[0]


async def _trigger(s: Serving, job: str) -> str:
    """Trigger ``job`` for today and return the run id."""
    return str((await s.call(lambda c: c.trigger(job, run_date=_today())))["run"]["run_id"])


class TestRestartWithAgent:
    """After a restart the scheduler knows its agents again and finishes their work once."""

    @pytest.mark.timeout(RESTART_TEST_SECONDS)
    def test_outside_broker_restart_keeps_capacity(self, tmp_path: Path) -> None:
        """Place pinned and tagged jobs after a restart that brought no new ``hello``."""

        async def main() -> None:
            """Run an outside broker and an agent across two lives of ``serve``."""
            server = BrokerServer()
            await server.start(HOST, 0)
            path = write_agents_config(tmp_path, server.port, outside=True, jobs=PLACED_JOBS)
            clock = now_clock()
            agent = make_service_agent(tmp_path, server.port, clock)
            try:
                first = await start_serve(path)
                try:
                    await agent.start()
                    await _online(first)
                finally:
                    await first.stop()
                second = await start_serve(path)
                try:
                    # The agent's line to the outside broker never dropped: no hello.
                    await until(lambda: clock.next_due() is not None)
                    await clock.advance(HEARTBEAT_EVERY)
                    await _online(second)
                    for job in ("pinned", "tagged"):
                        detail = await _ends(second, await _trigger(second, job), "succeeded")
                        [attempt] = detail["attempts"]
                        assert attempt["status_path"] == f"agent:{NAME}"
                finally:
                    await second.stop()
            finally:
                await agent.stop()
                await server.close()

        pynet.loop.run(main())

    @pytest.mark.timeout(RESTART_TEST_SECONDS)
    def test_embedded_broker_restart_mid_run(self, tmp_path: Path) -> None:
        """Finish a remote run that outlived ``serve`` once, with exactly one attempt."""
        marker = tmp_path / "marker"
        script = tmp_path / "job.sh"
        script.write_text(f"echo ran >> {marker}\nsleep {JOB_SECONDS}\n", encoding="utf-8")
        jobs = f"""
jobs:
  remote:
    command: "/bin/sh {script}"
    schedule: manual
    agent: {NAME}
"""
        port = free_port()
        path = write_agents_config(tmp_path, port, jobs=jobs)

        async def main() -> None:
            """Start the job, restart ``serve`` (and its broker) while it runs, wait for it."""
            agent: Agent = make_service_agent(tmp_path, port)
            first = await start_serve(path)
            try:
                await agent.start()
                await _online(first)
                run_id = await _trigger(first, "remote")
                await _ends(first, run_id, "running")
            finally:
                await first.stop()
            second = await start_serve(path)
            try:
                detail = await _ends(second, run_id, "succeeded")
                assert len(detail["attempts"]) == 1

                async def settled() -> bool:
                    """Return whether the agent has no unacked report left."""
                    return not agent.pending

                await _eventually(settled)
                assert not agent.running
            finally:
                await second.stop()
                await agent.stop()
            assert marker.read_text(encoding="utf-8").splitlines() == ["ran"]

        pynet.loop.run(main())
