"""A ready-made scheduler to look at: a config, a login and a few days of runs.

A shop window is more fun when the shelves are already stocked. This
module stocks the shelves of the web UI. ``seed_demo`` writes a small
config of twelve jobs (chains, a file watcher, a flaky job that needs a
second try, and a critical job that always fails), a ``users.yaml`` with
one user, ``admin`` / ``demo``, and then really runs the jobs for three
days with quick commands (``true``, ``false``, ``sleep 0.1``), so the
board, the graph and the timeline have something to show. ``serve_demo``
then opens the shop and waits for Ctrl-C.

The demo only ever listens on the loopback address, on a free port, and
the password ``demo`` is meant for exactly that: a toy on your own
machine.

Example::

    from pathlib import Path
from typing import cast
    from pyscheduler.demo import seed_demo, serve_demo

    seed_demo(Path("demo"))
    serve_demo(Path("demo"), seconds=60)
"""

import argparse
import hashlib
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pynet.loop
from pyweb.security import hash_password

from pyscheduler.client import ApiClient
from pyscheduler.engine.model import FINAL_STATES, RunState
from pyscheduler.service import serve

__all__ = ["LOGIN_NAME", "LOGIN_PASSWORD", "main", "seed_demo", "serve_demo"]

LOGIN_NAME = "admin"
"""The demo's only user."""

LOGIN_PASSWORD = "demo"  # noqa: S105 -- a toy password for a loopback-only demo
"""The demo user's password."""

CONFIG_NAME = "pysched.yaml"
"""The config file ``seed_demo`` writes in its folder."""

DAYS = 3
"""How many run dates (today and the days before) are seeded."""

READY_SECONDS = 15.0
"""How long ``serve`` may take to start listening."""

SETTLE_SECONDS = 20.0
"""How long the seeded runs may take to all come to rest."""

POLL = 0.05
"""How long the seeding loops nap (real seconds)."""

WATCH_POLL = "1s"
"""How often the demo's file watcher looks."""

RESTING = FINAL_STATES | {RunState.BLOCKED, RunState.HELD}
"""The states in which a seeded run counts as finished for the demo."""

ROOTS = ("extract", "fetch-rates", "inbox", "payments-export")
"""The jobs triggered for each seeded day; their dependents follow by themselves."""

JOBS = """
jobs:
  extract:
    description: Copy the day's trades out of the trading system.
    command: sleep 0.1
    schedule: manual
    tags: [etl]
    owner: data-team

  transform:
    description: Turn raw trades into warehouse rows.
    command: sleep 0.1
    depends_on: [extract]
    tags: [etl]
    owner: data-team

  fetch-rates:
    description: Download exchange rates; the server fails once, then answers.
    command: 'if test -e "{marks}/{{run_date}}"; then exit 0; else touch "{marks}/{{run_date}}"; exit 1; fi'
    schedule: manual
    retries: {{count: 2, delay: 1s}}
    tags: [etl, flaky]
    owner: data-team

  load:
    description: Load the rows, priced with the day's rates.
    command: sleep 0.1
    depends_on: [transform, fetch-rates]
    criticality: critical
    tags: [etl]
    owner: data-team

  report:
    description: Mail the morning report once the load is in.
    command: "true"
    depends_on: [load]
    tags: [etl, reports]
    owner: finance

  inbox:
    description: Wait for the day's prices file to arrive and sit still.
    type: file_watcher
    schedule: manual
    watch: "{inbox}/prices.csv"
    min_age: 0s
    poll: {watch_poll}
    give_up_after: 1m
    tags: [files]
    owner: finance

  parse-prices:
    description: Read the prices the watcher found.
    command: sleep 0.1
    depends_on: [inbox]
    tags: [files]
    owner: finance

  payments-export:
    description: Export the day's payments. It is broken, on purpose.
    command: "false"
    schedule: manual
    criticality: critical
    tags: [payments]
    owner: payments

  payments-notify:
    description: Tell the bank the export is ready (never starts if the export fails).
    command: "true"
    depends_on: [payments-export]
    tags: [payments]
    owner: payments

  payments-cleanup:
    description: Tidy up after a failed export.
    command: sleep 0.1
    depends_on:
      - {{job: payments-export, on: failure}}
    tags: [payments]
    owner: payments

  housekeeping:
    description: Nightly tidy-up of old files.
    command: sleep 0.1
    schedule: "0 3 * * *"
    catch_up: none
    tags: [ops]
    owner: platform

  weekly-audit:
    description: Monday morning audit (nothing has run yet; see the forecast).
    command: "true"
    schedule: "0 6 * * 1"
    catch_up: none
    tags: [ops]
    owner: platform
"""
"""The twelve jobs; ``{marks}``, ``{inbox}`` and ``{watch_poll}`` are filled in, ``{{run_date}}`` is left for the scheduler."""


def _config_text(folder: Path, token_file: Path, users_file: Path) -> str:
    """Return the text of the demo's config for ``folder``."""
    header = (
        "scheduler:\n"
        "  name: pyscheduler-demo\n"
        "  timezone: UTC\n"
        f"  state_dir: {folder / 'state'}\n"
        f"  log_dir: {folder / 'logs'}\n"
        "  listen: 127.0.0.1:0\n"
        "  max_running: 8\n"
        f"  api_tokens_file: {token_file}\n"
        f"  users_file: {users_file}\n"
    )
    jobs = JOBS.format(
        marks=folder / "marks", inbox=folder / "inbox", watch_poll=WATCH_POLL
    ).lstrip("\n")
    return header + jobs


def _wait_for_runs(client: ApiClient, days: Sequence[date]) -> int:
    """Poll until every run of ``days`` has come to rest; return how many runs there are.

    Raises:
        TimeoutError: If they have not all come to rest within ``SETTLE_SECONDS``.

    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline:
        states = [
            RunState(str(run["state"]))
            for day in days
            for run in cast(
                "list[dict[str, object]]",
                client.runs(run_date=day.isoformat(), limit=1000)["runs"],
            )
        ]
        # A trigger's dependents are created a moment later, so wait for the whole chain.
        if len(states) >= len(JOBS_PER_DAY) * len(days) and all(s in RESTING for s in states):
            return len(states)
        time.sleep(POLL)
    msg = "the demo runs did not finish"
    raise TimeoutError(msg)


JOBS_PER_DAY = ("extract", "transform", "fetch-rates", "load", "report", "inbox", "parse-prices")
"""The jobs that always get a run on a seeded day (the payments chain and housekeeping are extra)."""


def _seed_runs(url: str, token: str) -> None:
    """Trigger the root jobs for each of the last ``DAYS`` days and wait for them all."""
    client = ApiClient(url, token)
    today = date.fromisoformat(str(client.health()["today"]))
    days = [today - timedelta(days=back) for back in range(DAYS - 1, -1, -1)]
    for day in days:
        for job in ROOTS:
            client.trigger(job, run_date=day.isoformat())
    client.backfill("housekeeping", start=days[0].isoformat(), end=days[-1].isoformat())
    _wait_for_runs(client, days)


def seed_demo(folder: Path) -> Path:
    """Write the demo config and login into ``folder``, run a few days of jobs; return the config path.

    The folder gets ``pysched.yaml``, ``users.yaml`` (``admin`` / ``demo``,
    hashed now), a private tokens file used only to drive the seeding,
    and the scheduler's ``state/`` and ``logs/``. The jobs are real
    quick commands run by an in-process scheduler, which is stopped again
    before this returns, so the state can be served straight away.

    Raises:
        TimeoutError: If the scheduler does not start or the runs do not finish in time.

    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "marks").mkdir(exist_ok=True)
    (folder / "inbox").mkdir(exist_ok=True)
    (folder / "inbox" / "prices.csv").write_text("AAPL,190.1\nMSFT,410.2\n", encoding="utf-8")
    users_file = folder / "users.yaml"
    users_file.write_text(
        f"- {{name: {LOGIN_NAME}, password_hash: {hash_password(LOGIN_PASSWORD)}, role: admin}}\n",
        encoding="utf-8",
    )
    token = secrets.token_urlsafe(32)
    token_file = folder / "seed-tokens.yaml"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_file.write_text(f"- {{name: seeder, token_sha256: {digest}, role: operator}}\n", "utf-8")
    token_file.chmod(0o600)
    path = folder / CONFIG_NAME
    path.write_text(_config_text(folder, token_file, users_file), encoding="utf-8")

    async def main() -> None:
        """Start the scheduler, seed it from a worker thread, then stop it."""
        stop = pynet.loop.Event()
        ports: list[int] = []
        task = pynet.loop.spawn(serve(path, ready=ports.append, stop=stop), name="demo-seed")
        try:
            async with pynet.loop.timeout(READY_SECONDS):
                while not ports and not task.done():
                    await pynet.loop.sleep(POLL)
            if task.done():
                task.result()  # raises what serve raised
                msg = "the demo scheduler stopped before it was ready"
                raise RuntimeError(msg)
            url = f"http://127.0.0.1:{ports[0]}"
            await pynet.loop.run_in_thread(lambda: _seed_runs(url, token))
        finally:
            stop.set()
            await task

    pynet.loop.run(main())
    return path


def serve_demo(
    folder: Path, *, seconds: float | None = None, ready: Callable[[str], None] | None = None
) -> None:
    """Serve the demo in ``folder`` (seeding it first if it is empty) until Ctrl-C.

    It prints the web address and the login, and calls ``ready(url)`` once
    the scheduler is listening. With ``seconds`` it stops by itself after
    that long, which is how tests and PyStack use it.

    Raises:
        ConfigError: If the config in ``folder`` is damaged.
        ServiceError: If the scheduler refuses to start.

    """
    path = folder / CONFIG_NAME
    if not path.exists():
        seed_demo(folder)

    def listening(port: int) -> None:
        """Say where the UI is and how to log in; tell ``ready``."""
        url = f"http://127.0.0.1:{port}"
        sys.stdout.write(
            f"PyScheduler demo is running at {url}\n"
            f"Log in as {LOGIN_NAME} with password {LOGIN_PASSWORD}. Press Ctrl-C to stop.\n"
        )
        sys.stdout.flush()
        if ready is not None:
            ready(url)

    async def main() -> None:
        """Run ``serve`` until it ends, with a timer that stops it after ``seconds``."""
        stop = pynet.loop.Event()
        timer = None
        if seconds is not None:

            async def stop_later(delay: float) -> None:
                """Set ``stop`` once ``delay`` has passed."""
                await pynet.loop.sleep(delay)
                stop.set()

            timer = pynet.loop.spawn(stop_later(seconds), name="demo-timer")
        try:
            await serve(path, ready=listening, stop=stop)
        finally:
            if timer is not None:
                timer.cancel()

    pynet.loop.run(main())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the demo from the command line (``--seed-only``, ``--folder``, ``--seconds``)."""
    parser = argparse.ArgumentParser(description="Seed and serve the PyScheduler web UI demo.")
    parser.add_argument("--folder", type=Path, default=Path("ui-demo"), help="where the demo lives")
    parser.add_argument("--seed-only", action="store_true", help="seed, then exit")
    parser.add_argument("--seconds", type=float, default=None, help="stop serving after this long")
    args = parser.parse_args(argv)
    if args.seed_only:
        sys.stdout.write(f"{seed_demo(args.folder)}\n")
        return 0
    serve_demo(args.folder, seconds=args.seconds)
    return 0
