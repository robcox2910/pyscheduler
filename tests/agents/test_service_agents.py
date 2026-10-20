"""Test ``serve`` with an ``agents:`` section: a real embedded broker and a real agent."""

import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pynet.loop
import pytest
from agent_helpers import NAME, RUN_ID, SECRET, WAIT, now_clock
from hub_helpers import FakeAgent
from service_agent_helpers import Running, run_service, write_agents_config, write_secrets

from pyscheduler.agents.messages import ACK_TYPE, key_payload
from pyscheduler.client import ApiClient
from pyscheduler.service import ServiceError, serve

UNKNOWN = (RUN_ID, 7)
"""An attempt no run of this scheduler follows."""
WRONG_SECRET = "t" * 40
SETTLE = 0.3
STOP_SECONDS = 5.0
"""How long ``serve`` may take to stop with a remote attempt still running."""


def _today() -> str:
    """Return today's date in UTC (the run date of a triggered run)."""
    return datetime.now(UTC).date().isoformat()


async def _state(r: Running, run_id: str, wanted: str) -> dict[str, Any]:
    """Wait until run ``run_id`` is in state ``wanted``; return the run's detail."""
    async with pynet.loop.timeout(WAIT):
        while True:
            detail = await r.call(lambda c: c.run(run_id))
            if detail["run"]["state"] == wanted:
                return detail
            await pynet.loop.sleep(SETTLE / 10)


class TestServiceAgents:
    """The service starts the hub, places jobs on agents and shows them."""

    def test_job_runs_on_the_agent(self, tmp_path: Path) -> None:
        """A job naming the agent runs there; the API lists the agent online."""

        async def body(r: Running) -> None:
            """Wait for the agent, trigger the job and read its log."""
            await r.agent_online()
            run = (await r.call(lambda c: c.trigger("remote", run_date=_today())))["run"]
            detail = await _state(r, run["run_id"], "succeeded")
            [attempt] = detail["attempts"]
            assert attempt["status_path"] == f"agent:{NAME}"
            log = await r.call(lambda c: c.log(run["run_id"]))
            assert log["lines"] == ["hello from the agent"]
            assert str(log["path"]).startswith(f"{NAME}:")
            [listed] = (await r.call(ApiClient.agents))["agents"]
            assert (listed["name"], listed["online"]) == (NAME, True)

        run_service(tmp_path, body)

    def test_unknown_finished_is_acked_once_up(self, tmp_path: Path) -> None:
        """Recovery ran on a started hub: a ``finished`` nobody follows is acked and dropped."""

        async def body(r: Running) -> None:
            """Play the agent and report an attempt this scheduler never sent."""
            fake = FakeAgent(r.broker_port, now_clock())
            await fake.start()
            try:
                await fake.hello()
                await fake.finished(UNKNOWN)
                _, ack = await fake.wait_for(ACK_TYPE)
                assert ack.payload == key_payload(*UNKNOWN)
            finally:
                await fake.close()

        run_service(tmp_path, body, start_agent=False)

    def test_reload_swaps_the_secrets(self, tmp_path: Path) -> None:
        """An agent with a secret the scheduler does not know yet is refused until a reload."""

        async def body(r: Running) -> None:
            """Say hello with the right secret before and after fixing the secrets file."""
            fake = FakeAgent(r.broker_port, now_clock())
            await fake.start()
            try:
                await fake.hello()
                await pynet.loop.sleep(SETTLE)
                assert (await r.call(ApiClient.agents))["agents"] == []
                write_secrets(tmp_path, SECRET)
                await r.call(ApiClient.reload)
                await fake.hello()
                await r.agent_online()
            finally:
                await fake.close()

        run_service(tmp_path, body, secret=WRONG_SECRET, start_agent=False)

    def test_stops_with_a_remote_attempt_running(self, tmp_path: Path) -> None:
        """``serve`` stops promptly (engine first, then the hub) while an agent runs a job."""
        ended: list[float] = []

        async def body(r: Running) -> None:
            """Start the slow job and leave it running."""
            await r.agent_online()
            run = (await r.call(lambda c: c.trigger("slow", run_date=_today())))["run"]
            await _state(r, run["run_id"], "running")
            ended.append(time.monotonic())

        run_service(tmp_path, body)
        assert time.monotonic() - ended[0] < STOP_SECONDS

    def test_busy_broker_port_refuses_to_start(self, tmp_path: Path) -> None:
        """An embedded broker that cannot listen stops ``serve`` with a ``ServiceError``."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            path = write_agents_config(tmp_path, taken.getsockname()[1])
            with pytest.raises(ServiceError, match="agents: cannot start the broker"):
                pynet.loop.run(serve(path))
