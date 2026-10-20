"""The ``pysched`` command line: run the scheduler, or talk to a running one.

``pysched`` is the remote control for the scheduler. A few buttons work
without the scheduler switched on (``validate``, ``forecast``,
``hash-token`` and ``hash-password`` only work here); ``serve`` switches it on; every
other button sends a request to the scheduler's HTTP API.

Where the API is: ``--url`` (or ``PYSCHED_URL``, default
``http://127.0.0.1:8470``). Who you are: ``--token`` (or
``PYSCHED_TOKEN``).

Exit codes: 0 ok, 1 the server said no (or could not be reached), 2 a
usage or validation error.

Example::

    pysched validate scheduler.yaml
    pysched serve scheduler.yaml &
    pysched trigger extract
    pysched status
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pynet.loop
from pydb.errors import PyDBError

from pyscheduler.cli_alerts import ALERT_COMMANDS
from pyscheduler.cli_commands import (
    CLIENT_COMMANDS,
    OFFLINE_COMMANDS,
    OK,
    SERVER_ERROR,
    USAGE,
    problem_lines,
    warn,
)
from pyscheduler.client import DEFAULT_URL, RUN_ACTIONS, ApiClient, ApiError
from pyscheduler.config import ConfigError
from pyscheduler.service import SchedulerFailedError, ServiceError, serve
from pyscheduler.store import StoreSchemaError

__all__ = ["LOG_FORMAT", "build_parser", "main"]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""How each line of the scheduler's own log looks."""

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

INTERRUPTED = 130
"""The exit code for Ctrl-C before ``serve`` could take it over (128 + SIGINT)."""

type AddParser = Callable[..., argparse.ArgumentParser]
"""``add_parser`` of the subcommands: add one subcommand and return its parser."""

_RUN_HELP = {
    "rerun": "reopen a final run",
    "hold": "freeze a waiting run",
    "release": "unfreeze a held run",
    "kill": "stop a running run",
    "force-start": "start a run now, ignoring its dependencies",
    "skip": "skip a run that has not started",
    "mark-success": "mark a run as succeeded",
    "mark-failed": "mark a run as failed",
}


def _connection(*, suppress: bool) -> argparse.ArgumentParser:
    """Return the ``--url``/``--token`` options (``suppress``: no default, for subcommands)."""
    parent = argparse.ArgumentParser(add_help=False)
    default = argparse.SUPPRESS if suppress else None
    parent.add_argument("--url", default=default, help="the scheduler API (env PYSCHED_URL)")
    parent.add_argument(
        "--token",
        default=default,
        help="a bearer token (prefer env PYSCHED_TOKEN: --token shows in ps)",
    )
    return parent


def _add_offline(add: AddParser) -> None:
    """Add ``validate``, ``serve``, ``forecast``, ``hash-token`` and ``hash-password``."""
    validate = add("validate", help="check a config file")
    validate.add_argument("file", type=Path)
    serve_cmd = add("serve", help="run the scheduler")
    serve_cmd.add_argument("file", type=Path)
    serve_cmd.add_argument("--log-level", default="INFO", choices=LOG_LEVELS)
    serve_cmd.add_argument("--log-file", type=Path, help="also log to this file")
    forecast = add("forecast", help="predict runs from a config (offline)")
    forecast.add_argument("file", type=Path)
    forecast.add_argument("--from", dest="start", help="date or ISO instant (default now)")
    forecast.add_argument("--to", dest="end", help="date (inclusive) or ISO instant")
    add("hash-token", help="make a new API token and its sha256")
    add("hash-password", help="hash a password for the users file")


def _add_client(add: AddParser) -> None:
    """Add the commands that talk to a running scheduler."""
    conn = [_connection(suppress=True)]
    status = add("status", parents=conn, help="one run date's runs")
    status.add_argument("--date", help="run date YYYY-MM-DD (default today)")
    runs = add("runs", parents=conn, help="list runs")
    runs.add_argument("--job")
    runs.add_argument("--state")
    runs.add_argument("--date")
    runs.add_argument("--limit", type=int)
    show = add("show", parents=conn, help="a run with attempts and events")
    show.add_argument("run")
    log = add("log", parents=conn, help="the end of a run's output")
    log.add_argument("run")
    log.add_argument("--stderr", action="store_true", help="show stderr instead of stdout")
    log.add_argument("--tail", type=int, help="how many lines (default 200)")
    log.add_argument("--attempt", type=int, help="which attempt (default the latest)")
    trigger = add("trigger", parents=conn, help="start a manual run of a job")
    trigger.add_argument("job")
    trigger.add_argument(
        "--run-date",
        "--date",
        dest="run_date",
        help="run date YYYY-MM-DD (default: what the job's run_date rule gives today)",
    )
    backfill = add("backfill", parents=conn, help="create runs for past days")
    backfill.add_argument("job")
    backfill.add_argument("--from", dest="start", required=True, help="first run date")
    backfill.add_argument("--to", dest="end", required=True, help="last run date")
    for action in RUN_ACTIONS:
        command = add(action, parents=conn, help=_RUN_HELP[action])
        command.add_argument("run")
        if action == "rerun":
            command.add_argument(
                "--downstream", action="store_true", help="also rerun final downstream runs"
            )
    add("reload", parents=conn, help="reload the config files")
    add("agents", parents=conn, help="list the agents: online or not, load and tags")
    alerts = add("alerts", parents=conn, help="list alerts and how their delivery went")
    alerts.add_argument("--kind", help="only this kind of alert, such as failed")
    alerts.add_argument("--job", help="only this job's alerts")
    alerts.add_argument("--since", help="only alerts raised on or after this date YYYY-MM-DD")
    alerts.add_argument("--dead", action="store_true", help="list the deliveries that gave up")
    replay = add("replay", parents=conn, help="send an alert's dead deliveries again")
    replay.add_argument("alert", help="the alert id")
    replay.add_argument("--notifier", help="only this notifier's delivery")


def build_parser() -> argparse.ArgumentParser:
    """Return the ``pysched`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pysched",
        description="Run PyScheduler, or talk to a running one.",
        parents=[_connection(suppress=False)],
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    _add_offline(commands.add_parser)
    _add_client(commands.add_parser)
    return parser


def _log_handlers(file: Path | None) -> list[logging.Handler]:
    """Return the stderr handler (and one for ``file``) with the log format.

    Raises:
        OSError: If ``file`` cannot be opened.

    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file is not None:
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
    return handlers


@contextmanager
def _logging(level: str, handlers: list[logging.Handler]) -> Generator[None]:
    """Attach ``handlers`` to the root logger at ``level`` while serving, then take them away."""
    root = logging.getLogger()
    before = root.level
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    try:
        yield
    finally:
        root.setLevel(before)
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()


def _serve(args: argparse.Namespace) -> int:
    """Run ``serve`` until it is stopped; turn its refusals into one line and an exit code.

    Usage errors (2): a config with errors, an unsafe address, a busy
    port, a bad tokens file or log file. Failures (1): the store cannot
    be opened (locked, another layout, no folder) or the engine failed.
    Ctrl-C before the signal handlers are hooked exits 130.
    """
    try:
        handlers = _log_handlers(args.log_file)
    except OSError as exc:
        warn(f"pysched serve: cannot open log file: {exc}")
        return USAGE
    try:
        with _logging(args.log_level, handlers):
            pynet.loop.run(serve(args.file))
    except ConfigError as exc:
        for problem in exc.problems:
            warn(str(problem))
        return USAGE
    except ServiceError as exc:
        warn(f"pysched serve: {exc}")
        return USAGE
    except (PyDBError, StoreSchemaError, SchedulerFailedError, OSError) as exc:
        warn(f"pysched serve: {exc}")
        return SERVER_ERROR
    except KeyboardInterrupt:
        warn("pysched serve: interrupted")
        return INTERRUPTED
    return OK


def _client(args: argparse.Namespace) -> ApiClient:
    """Return a client for the ``--url``/``--token`` options or their environment variables."""
    url = args.url or os.environ.get("PYSCHED_URL") or DEFAULT_URL
    token = args.token or os.environ.get("PYSCHED_TOKEN") or None
    return ApiClient(url, token)


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``pysched`` with ``argv`` (default ``sys.argv[1:]``) and return its exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help (0) or a usage error (2)
        return exc.code if isinstance(exc.code, int) else USAGE
    command: str = args.command
    if command == "serve":
        return _serve(args)
    offline = OFFLINE_COMMANDS.get(command)
    if offline is not None:
        return offline(args)
    try:
        return (CLIENT_COMMANDS | ALERT_COMMANDS)[command](_client(args), args)
    except ApiError as exc:
        warn(f"pysched {command}: {exc}")
        for line in problem_lines(exc.problems):
            warn(f"  {line}")
        return SERVER_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
