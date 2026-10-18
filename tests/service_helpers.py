"""Helpers for the service and CLI tests: write a config and run ``serve`` in-process."""

import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop

from pyscheduler.client import ApiClient, ApiError
from pyscheduler.service import serve

READY_SECONDS = 5.0
"""How long ``serve`` may take to start listening."""

DONE_SECONDS = 5.0
"""How long a test waits for runs of ``true`` to end."""

POLL = 0.02
"""How long the polling loops nap (real seconds)."""

JOBS = """
jobs:
  a:
    command: "true"
    schedule: manual
  b:
    command: "true"
    depends_on: [a]
  gate:
    command: "true"
    schedule: manual
  waiter:
    command: "true"
    depends_on: [gate]
"""
"""``a`` then ``b``; ``waiter`` waits on ``gate``, so a triggered ``waiter`` can be held."""


def write_config(
    folder: Path, jobs: str = JOBS, *, listen: str = "127.0.0.1:0", extra: str = ""
) -> Path:
    """Write ``pysched.yaml`` in ``folder`` and return its path."""
    header = (
        "scheduler:\n"
        f"  state_dir: {folder / 'state'}\n"
        f"  log_dir: {folder / 'logs'}\n"
        f"  listen: {listen}\n" + extra
    )
    path = folder / "pysched.yaml"
    path.write_text(header + textwrap.dedent(jobs).lstrip("\n"), encoding="utf-8")
    return path


def today() -> str:
    """Return today's date in UTC (the run date of a triggered run) as ``YYYY-MM-DD``."""
    return datetime.now(UTC).date().isoformat()


def with_service[T](path: Path, body: Callable[[str], T]) -> T:
    """Run ``serve(path)`` on this (main) thread's loop and ``body(url)`` on a worker thread.

    ``serve`` is stopped through its ``stop`` event once ``body`` returns
    (or raises), and the body's result is returned.
    """

    async def main() -> T:
        """Start serve, wait until it listens, run the body, then stop serve."""
        stop = pynet.loop.Event()
        ports: list[int] = []
        task = pynet.loop.spawn(serve(path, ready=ports.append, stop=stop), name="serve")
        async with pynet.loop.timeout(READY_SECONDS):
            while not ports and not task.done():
                await pynet.loop.sleep(POLL)
        if task.done():
            task.result()  # raises what serve raised
            msg = "serve ended before it was ready"
            raise RuntimeError(msg)
        try:
            return await pynet.loop.run_in_thread(lambda: body(f"http://127.0.0.1:{ports[0]}"))
        finally:
            stop.set()
            await task

    return pynet.loop.run(main())


def wait_for_states(
    client: ApiClient, wanted: dict[str, str], *, run_date: str | None = None
) -> dict[str, str]:
    """Poll ``client.runs`` until each job's newest run is in its wanted state; return them all.

    Raises:
        AssertionError: If that does not happen within ``DONE_SECONDS``.

    """
    deadline = time.monotonic() + DONE_SECONDS
    states: dict[str, str] = {}
    while time.monotonic() < deadline:
        runs = client.runs(run_date=run_date or today())["runs"]
        states = {}
        for run in sorted(runs, key=lambda r: (r["created_at"], r["run_id"])):
            states[run["job"]] = run["state"]  # the newest run of a job wins
        if all(states.get(job) == state for job, state in wanted.items()):
            return states
        time.sleep(POLL)
    msg = f"runs did not reach {wanted}: {states}"
    raise AssertionError(msg)


def idle(url: str) -> None:
    """Do nothing with a running service (a body for ``with_service``)."""
    del url


def reachable(client: ApiClient) -> bool:
    """Return whether the scheduler answers ``/api/health`` (status 0 means nobody is there)."""
    try:
        client.health()
    except ApiError as exc:
        if exc.status != 0:
            raise
        return False
    return True
