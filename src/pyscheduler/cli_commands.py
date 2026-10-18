"""The ``pysched`` subcommands: each takes the parsed arguments and returns an exit code.

Offline commands (``validate``, ``forecast``, ``hash-token``) read files
on this machine. The rest ring the scheduler's API through an
``ApiClient``; an ``ApiError`` they raise is turned into exit code 1 by
``cli.main``.
"""

import argparse
import hashlib
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pyscheduler.cli_format import (
    RUN_HEADER,
    detail,
    duration,
    elapsed,
    run_rows,
    table,
    text,
)
from pyscheduler.client import RUN_ACTIONS, ApiClient
from pyscheduler.config import Config, ConfigError, Problem, load_config
from pyscheduler.schedule.forecast import forecast

__all__ = ["CLIENT_COMMANDS", "OFFLINE_COMMANDS", "OK", "SERVER_ERROR", "USAGE", "local_today"]

OK = 0
SERVER_ERROR = 1
USAGE = 2

TOKEN_BYTES = 32
"""How many random bytes a new token has (before URL-safe base64)."""

MAX_FORECAST = timedelta(days=366)
"""The longest window ``forecast`` predicts."""

ONE_DAY = timedelta(days=1)

STATUS_LIMIT = 1000
"""The most runs ``status`` shows (the API's largest page)."""


def say(line: str = "") -> None:
    """Print one line to stdout."""
    sys.stdout.write(line + "\n")


def warn(line: str) -> None:
    """Print one line to stderr."""
    sys.stderr.write(line + "\n")


def local_today() -> str:
    """Return today's date on this machine as ``YYYY-MM-DD``."""
    return datetime.now(UTC).astimezone().date().isoformat()


# -- offline -----------------------------------------------------------


def _load(path: Path) -> Config | list[Problem]:
    """Load the config at ``path``, or return its problems."""
    try:
        return load_config(path)
    except ConfigError as exc:
        return list(exc.problems)


def validate(args: argparse.Namespace) -> int:
    """Check a config: errors to stdout (exit 2), warnings to stderr, else ``ok``."""
    loaded = _load(args.file)
    problems = loaded if isinstance(loaded, list) else list(loaded.warnings)
    for problem in problems:
        if problem.severity == "error":
            say(str(problem))
        else:
            warn(str(problem))
    if isinstance(loaded, list) and any(p.severity == "error" for p in problems):
        return USAGE
    if not isinstance(loaded, list):
        count = len(loaded.jobs)
        say(f"ok: {args.file} has {count} job{'s' if count != 1 else ''}")
    return OK


def _instant(value: str, zone: ZoneInfo, *, end: bool) -> datetime:
    """Return a ``--from``/``--to`` value as an aware instant.

    A value without a time part (any ISO date form, such as
    ``2026-10-19`` or ``20261019``) is a date: midnight in ``zone``, or
    for ``--to`` the end of that day. Anything else is an ISO instant; one
    without an offset is read in ``zone``.

    Raises:
        ValueError: If it is neither an ISO date nor an ISO instant.

    """
    try:
        day = date.fromisoformat(value)
    except ValueError:
        moment = datetime.fromisoformat(value)
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=zone)
    return datetime.combine(day + (ONE_DAY if end else timedelta(0)), time(), zone)


def forecast_offline(args: argparse.Namespace) -> int:
    """Print the runs the config predicts between ``--from`` and ``--to`` (no durations)."""
    loaded = _load(args.file)
    if isinstance(loaded, list):
        for problem in loaded:
            warn(str(problem))
        return USAGE
    zone = loaded.settings.timezone
    try:
        start = _instant(args.start, zone, end=False) if args.start else datetime.now(UTC)
        stop = _instant(args.end, zone, end=True) if args.end else start + ONE_DAY
    except ValueError as exc:
        warn(f"pysched forecast: {exc}")
        return USAGE
    if stop < start:
        warn("pysched forecast: --to is before --from")
        return USAGE
    if stop - start > MAX_FORECAST:
        warn(f"pysched forecast: the window may be at most {MAX_FORECAST.days} days")
        return USAGE
    rows = [
        [
            e.job,
            e.run_date.isoformat(),
            e.expected_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            e.via,
        ]
        for e in forecast(loaded, start, stop)
    ]
    say(table(("JOB", "RUN_DATE", "EXPECTED_START", "VIA"), rows))
    return OK


def hash_token(args: argparse.Namespace) -> int:
    """Print a new random token and its SHA-256 (the line for the tokens file)."""
    del args
    token = secrets.token_urlsafe(TOKEN_BYTES)
    say(f"token: {token}")
    say(f"token_sha256: {hashlib.sha256(token.encode('utf-8')).hexdigest()}")
    return OK


OFFLINE_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "validate": validate,
    "forecast": forecast_offline,
    "hash-token": hash_token,
}


# -- against the server ------------------------------------------------


def _server_today(client: ApiClient) -> str:
    """Return today's date where the scheduler is (this machine's date for an older one)."""
    today = client.health().get("today")
    return today if isinstance(today, str) else local_today()


def status(client: ApiClient, args: argparse.Namespace) -> int:
    """Print one run date's runs: job, run id, state, attempt, started, duration.

    The run date defaults to today in the scheduler's time zone.
    """
    run_date = args.date or _server_today(client)
    runs = client.runs(run_date=run_date, limit=STATUS_LIMIT)["runs"]
    rows: list[list[object]] = []
    for run in sorted(runs, key=lambda r: (r["job"], r["run_id"])):
        started = run.get("started_at")
        seconds = run.get("duration_seconds")
        shown = duration(seconds)
        if seconds is None and started and run["state"] == "running":
            shown = duration(elapsed(started)) + "+"
        rows.append([run["job"], run["run_id"], run["state"], run["attempt"], started, shown])
    say(table(("JOB", "RUN", "STATE", "ATTEMPT", "STARTED", "DURATION"), rows))
    if len(runs) >= STATUS_LIMIT:
        warn(f"(only the newest {STATUS_LIMIT} runs of {run_date} are shown; use runs --job)")
    return OK


def runs(client: ApiClient, args: argparse.Namespace) -> int:
    """Print runs filtered by ``--job``, ``--state``, ``--date`` and ``--limit``."""
    found = client.runs(run_date=args.date, job=args.job, state=args.state, limit=args.limit)
    say(table(RUN_HEADER, run_rows(found["runs"])))
    return OK


def show(client: ApiClient, args: argparse.Namespace) -> int:
    """Print one run with its attempts and events."""
    say(detail(client.run(args.run)))
    return OK


def log(client: ApiClient, args: argparse.Namespace) -> int:
    """Print the end of an attempt's stdout (or ``--stderr``)."""
    stream = "stderr" if args.stderr else "stdout"
    answer = client.log(args.run, stream=stream, tail=args.tail, attempt=args.attempt)
    for line in answer["lines"]:
        say(str(line).rstrip("\n"))
    if answer.get("truncated"):
        warn(f"(only the last lines of {answer['path']} are shown)")
    return OK


def _print_runs(found: list[dict[str, object]]) -> None:
    """Print one ``run_id: state`` line per run (or a note when there are none)."""
    if not found:
        say("no runs")
    for run in found:
        say(f"{run['run_id']}: {run['state']}")


def trigger(client: ApiClient, args: argparse.Namespace) -> int:
    """Create a manual run of a job."""
    _print_runs([client.trigger(args.job, run_date=args.run_date)["run"]])
    return OK


def backfill(client: ApiClient, args: argparse.Namespace) -> int:
    """Create runs of a job for every business day ``--from`` .. ``--to``."""
    _print_runs(client.backfill(args.job, start=args.start, end=args.end)["runs"])
    return OK


def run_action(client: ApiClient, args: argparse.Namespace) -> int:
    """Run one operator action (``hold``, ``kill``, ...) on a run."""
    answer = client.run_action(
        args.run, args.command, downstream=getattr(args, "downstream", False)
    )
    _print_runs(answer["runs"] if "runs" in answer else [answer["run"]])
    return OK


def reload(client: ApiClient, args: argparse.Namespace) -> int:
    """Ask the scheduler to reload its config files; warnings go to stderr."""
    del args
    answer = client.reload()
    for problem in answer.get("warnings", []):
        warn(_problem_text(problem))
    say("reloaded")
    return OK


def _problem_text(problem: dict[str, object]) -> str:
    """Return a problem from the API the way ``str(Problem)`` writes it."""
    job = problem.get("job")
    where = f"{problem.get('file')}:{problem.get('line')}: "
    return where + (f"job {job}: " if job else "") + text(problem.get("message"))


def problem_lines(problems: list[dict[str, object]]) -> list[str]:
    """Return the ``file:line: message`` lines of an API error's problems."""
    return [_problem_text(p) for p in problems]


CLIENT_COMMANDS: dict[str, Callable[[ApiClient, argparse.Namespace], int]] = {
    "status": status,
    "runs": runs,
    "show": show,
    "log": log,
    "trigger": trigger,
    "backfill": backfill,
    "reload": reload,
    **dict.fromkeys(RUN_ACTIONS, run_action),
}
