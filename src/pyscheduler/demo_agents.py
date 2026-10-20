"""A two-agent demo: one scheduler, two remote workers and two jobs placed on them.

Picture a small building site with two builders, ``build-01`` and
``gpu-01``. The foreman (the scheduler) keeps a list of what each builder
can do. One job says "give this to build-01" (it is *pinned*); the other
says "give this to whoever can do ``gpu``" (it is *tagged*). The foreman
posts each order through the broker, the builders do the work, and the
demo prints which builder ran which job.

``run_agents_demo`` writes everything it needs into a folder (a config
with an embedded broker, the agents' secrets, two jobs), starts the
scheduler and both agents on one event loop, triggers the two jobs, waits
for them to finish and prints the verdict. The broker listens on
loopback only and has no password of its own: the agents' signed
messages are the protection, which is why the demo never leaves your
machine.

It runs real child processes, and pynet watches children only from the
main thread, so call it there.

Example::

    from pathlib import Path
    from pyscheduler.demo_agents import run_agents_demo

    run_agents_demo(Path("agents-demo"))
"""

import argparse
import secrets
import socket
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pynet.loop

from pyscheduler.agents.worker import Agent
from pyscheduler.client import ApiClient
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.model import FINAL_STATES, RunState
from pyscheduler.service import serve

__all__ = ["AGENTS", "PINNED_JOB", "TAGGED_JOB", "DemoAgent", "main", "run_agents_demo"]

HOST = "127.0.0.1"
"""The only address the demo's scheduler and broker listen on."""

PINNED_JOB = "build-app"
"""The job that names its agent."""

TAGGED_JOB = "train-model"
"""The job that asks for any agent with the ``gpu`` tag."""

DEFAULT_SECONDS = 60.0
"""How long the whole demo may take when ``seconds`` is not given."""

POLL = 0.05
"""How long the waiting loops nap (real seconds)."""

SLOTS = 2
"""How many jobs each demo agent may run at once."""

SECRETS_MODE = 0o600
"""The secrets file is for its owner only."""


@dataclass(frozen=True)
class DemoAgent:
    """One worker of the demo: its name and what it offers."""

    name: str
    tags: frozenset[str]


AGENTS = (
    DemoAgent("build-01", frozenset({"linux"})),
    DemoAgent("gpu-01", frozenset({"linux", "gpu"})),
)
"""The two agents the demo starts."""

CONFIG = """\
scheduler:
  state_dir: {state}
  log_dir: {logs}
  listen: {host}:0

agents:
  listen: "{host}:{broker_port}"
  secrets_file: agents.yaml

jobs:
  {pinned}:
    description: Build the app on one named agent.
    command: echo building the app
    schedule: manual
    agent: build-01
  {tagged}:
    description: Train a model on any agent that has a gpu.
    command: echo training the model
    schedule: manual
    agent_tags: [gpu]
"""
"""The demo's config: an embedded broker, a pinned job and a tagged job."""


def _free_port() -> int:
    """Return a TCP port on loopback that nothing listens on right now."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        port: int = probe.getsockname()[1]
    return port


def _write_files(folder: Path, broker_port: int) -> tuple[Path, dict[str, str]]:
    """Write the config and the secrets file; return the config path and each agent's secret."""
    folder.mkdir(parents=True, exist_ok=True)
    keys = {agent.name: secrets.token_hex(20) for agent in AGENTS}
    book = folder / "agents.yaml"
    book.write_text(
        "".join(f"- {{name: {name}, secret: {key}}}\n" for name, key in keys.items()),
        encoding="utf-8",
    )
    book.chmod(SECRETS_MODE)
    path = folder / "pysched.yaml"
    path.write_text(
        CONFIG.format(
            state=folder / "state",
            logs=folder / "logs",
            host=HOST,
            broker_port=broker_port,
            pinned=PINNED_JOB,
            tagged=TAGGED_JOB,
        ),
        encoding="utf-8",
    )
    return path, keys


def _make_agents(folder: Path, broker_port: int, keys: dict[str, str]) -> list[Agent]:
    """Return the demo's agents (not started), each with its own state and log folders."""
    return [
        Agent(
            name=agent.name,
            tags=agent.tags,
            secret=keys[agent.name],
            broker=(HOST, broker_port),
            state_dir=folder / "agents" / agent.name / "state",
            log_dir=folder / "agents" / agent.name / "logs",
            max_running=SLOTS,
            clock=SystemClock(),
        )
        for agent in AGENTS
    ]


def _online(client: ApiClient) -> bool:
    """Return whether the API lists every demo agent online."""
    listed = cast("list[dict[str, object]]", client.agents()["agents"])
    online = {str(a["name"]) for a in listed if a["online"]}
    return online >= {agent.name for agent in AGENTS}


def _wait_until(check: Callable[[], bool], what: str, deadline: float) -> None:
    """Poll ``check`` (never a blind sleep) until it is true.

    Raises:
        TimeoutError: If it is still false at ``deadline`` (``time.monotonic``).

    """
    while not check():
        if time.monotonic() >= deadline:
            msg = f"the agents demo timed out waiting for {what}"
            raise TimeoutError(msg)
        time.sleep(POLL)


def _run_jobs(url: str, seconds: float) -> dict[str, str]:
    """Trigger both jobs through the API; return which agent ran each (job name to agent name).

    Runs on a helper thread because the client blocks.

    Raises:
        TimeoutError: If the agents do not come online or a job does not
            succeed within ``seconds``.
        RuntimeError: If a job ends in a state other than ``succeeded``.

    """
    deadline = time.monotonic() + seconds
    client = ApiClient(url, None)
    _wait_until(lambda: _online(client), "the agents to come online", deadline)
    today = datetime.now(UTC).date().isoformat()
    run_ids = {
        job: str(client.trigger(job, run_date=today)["run"]["run_id"])
        for job in (PINNED_JOB, TAGGED_JOB)
    }
    ran_on: dict[str, str] = {}

    def settled(job: str) -> bool:
        """Return whether ``job``'s run is over; note where it ran if it succeeded."""
        detail = client.run(run_ids[job])
        state = str(detail["run"]["state"])
        if state == RunState.SUCCEEDED:
            [attempt] = detail["attempts"]
            ran_on[job] = str(attempt["status_path"]).removeprefix("agent:")
            return True
        if RunState(state) in FINAL_STATES:
            msg = f"job {job} ended {state}"
            raise RuntimeError(msg)
        return False

    for job in run_ids:
        _wait_until(lambda job=job: settled(job), f"job {job}", deadline)
    return ran_on


async def _main(
    path: Path,
    agents: list[Agent],
    seconds: float,
    ready: Callable[[str], None] | None,
) -> dict[str, str]:
    """Run ``serve`` and the agents; drive the jobs; stop everything; return where each ran."""
    stop = pynet.loop.Event()
    ports: list[int] = []
    task = pynet.loop.spawn(serve(path, ready=ports.append, stop=stop), name="agents-demo")
    started: list[Agent] = []
    try:
        async with pynet.loop.timeout(seconds):
            while not ports and not task.done():
                await pynet.loop.sleep(POLL)
        if task.done():
            task.result()  # raises what serve raised
            msg = "the demo scheduler stopped before it was ready"
            raise RuntimeError(msg)
        url = f"http://{HOST}:{ports[0]}"
        for agent in agents:
            await agent.start()
            started.append(agent)
        if ready is not None:
            ready(url)
        return await pynet.loop.run_in_thread(lambda: _run_jobs(url, seconds))
    finally:
        try:
            for agent in reversed(started):
                await agent.stop()
        finally:  # the scheduler stops even if an agent's stop fails
            stop.set()
            await task


def run_agents_demo(
    folder: Path, *, seconds: float | None = None, ready: Callable[[str], None] | None = None
) -> None:
    """Run the two-agent demo in ``folder``, print which agent ran each job, and stop.

    It writes the config and the agents' secrets (fresh random ones each
    time), starts the scheduler and both agents on one loop, triggers the
    pinned job and the tagged job, waits for both to succeed and prints
    the agent that ran each. ``ready(url)`` is called once the scheduler
    is listening and both agents have started. ``seconds`` is the longest
    the whole thing may take (default ``DEFAULT_SECONDS``). Call it on the
    main thread: the jobs are child processes.

    Raises:
        RuntimeError: If a job fails.
        TimeoutError: If the agents or the jobs take longer than ``seconds``.
        ServiceError: If the scheduler refuses to start.

    """
    folder = folder.absolute()
    limit = DEFAULT_SECONDS if seconds is None else seconds
    broker_port = _free_port()
    path, keys = _write_files(folder, broker_port)
    agents = _make_agents(folder, broker_port, keys)
    ran_on = pynet.loop.run(_main(path, agents, limit, ready))
    for job in (PINNED_JOB, TAGGED_JOB):
        sys.stdout.write(f"{job} ran on {ran_on[job]}\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the demo from the command line (``--folder``, ``--seconds``)."""
    parser = argparse.ArgumentParser(description="Run the PyScheduler two-agent demo.")
    parser.add_argument("--folder", type=Path, default=Path("agents-demo"), help="where it lives")
    parser.add_argument("--seconds", type=float, default=None, help="give up after this long")
    args = parser.parse_args(argv)
    run_agents_demo(args.folder, seconds=args.seconds)
    return 0
