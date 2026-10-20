"""The ``pysched-agent`` command line: run a remote worker for the scheduler.

Start one on every machine that should run jobs, each with its own name
and the secret the scheduler's ``agents.yaml`` holds for that name::

    pysched-agent --broker sched.example:8471 --name build-01 --tags linux,gpu --secret-file s.txt

The secret file holds just the secret (at least 32 characters) and
should be readable by its owner only. The secret is never printed.

Exit codes: 0 stopped cleanly, 1 the agent failed, 2 a usage error (a
bad option, name, tag or secret file).
"""

import argparse
import logging
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

import pynet.loop
from pydb.errors import PyDBError
from pymq.broker import BrokerError
from pynet.loop import Future

from pyscheduler.agents.worker import Agent
from pyscheduler.config.agent_secrets import MIN_SECRET_LENGTH
from pyscheduler.engine.clock import Clock, SystemClock
from pyscheduler.service_parts import SignalHooks
from pyscheduler.values import check_name

__all__ = ["AgentUsageError", "build_agent", "build_parser", "main", "read_secret"]

OK = 0
FAILED = 1
USAGE = 2
INTERRUPTED = 130
DEFAULT_MAX_RUNNING = 4
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
OTHERS_MAY_READ = stat.S_IRWXG | stat.S_IRWXO

logger = logging.getLogger("pyscheduler.agents.cli")


class AgentUsageError(ValueError):
    """Raised for a bad option value; the message never holds the secret."""


def build_parser() -> argparse.ArgumentParser:
    """Return the ``pysched-agent`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pysched-agent", description="Run jobs for a PyScheduler over a pymq broker."
    )
    parser.add_argument("--broker", required=True, help="the broker as host:port")
    parser.add_argument("--name", required=True, help="this agent's name")
    parser.add_argument("--tags", default="", help="comma-separated tags, e.g. linux,gpu")
    parser.add_argument("--secret-file", required=True, type=Path, help="file holding the secret")
    parser.add_argument(
        "--max-running", type=int, default=DEFAULT_MAX_RUNNING, help="attempts run at once"
    )
    parser.add_argument("--state-dir", type=Path, help="default ./agent-state/<name>")
    parser.add_argument("--log-dir", type=Path, help="default ./agent-logs/<name>")
    return parser


def read_secret(path: Path) -> str:
    """Return the secret in ``path`` (surrounding whitespace removed).

    A file the group or others may read is used, with a warning.

    Raises:
        AgentUsageError: If the file cannot be read or the secret is too short.

    """
    try:
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read secret file: {exc}"
        raise AgentUsageError(msg) from None
    secret = text.strip()
    if len(secret) < MIN_SECRET_LENGTH:
        msg = f"the secret in {path} must be at least {MIN_SECRET_LENGTH} characters"
        raise AgentUsageError(msg)
    if mode & OTHERS_MAY_READ:
        logger.warning("secret file %s is readable by group or others; chmod 600 it", path)
    return secret


def _broker(text: str) -> tuple[str, int]:
    """Return ``(host, port)`` from ``host:port``.

    Raises:
        AgentUsageError: If it is not in that form.

    """
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        msg = f"--broker must be host:port, not {text!r}"
        raise AgentUsageError(msg)
    return host, int(port)


def _names(name: str, tags: str) -> frozenset[str]:
    """Check the agent name and return the tags.

    Raises:
        AgentUsageError: If the name or a tag breaks the job-name rule.

    """
    found = frozenset(tag.strip() for tag in tags.split(",") if tag.strip())
    try:
        check_name(name)
        for tag in sorted(found):
            check_name(tag)
    except ValueError as exc:
        raise AgentUsageError(str(exc)) from None
    return found


def build_agent(argv: Sequence[str] | None, *, clock: Clock) -> Agent:
    """Return the agent the command line ``argv`` describes.

    Raises:
        AgentUsageError: If an option value is bad.
        SystemExit: From ``argparse``, for ``--help`` or a usage error.

    """
    args = build_parser().parse_args(argv)
    name: str = args.name
    tags = _names(name, args.tags)
    if args.max_running < 1:
        msg = "--max-running must be at least 1"
        raise AgentUsageError(msg)
    broker = _broker(args.broker)
    secret = read_secret(args.secret_file)
    return Agent(
        name=name,
        tags=tags,
        secret=secret,
        broker=broker,
        state_dir=args.state_dir or Path("agent-state") / name,
        log_dir=args.log_dir or Path("agent-logs") / name,
        max_running=args.max_running,
        clock=clock,
    )


async def run_agent(agent: Agent) -> None:
    """Start ``agent`` and run it until SIGTERM or SIGINT, then stop it.

    The agent starts in a task, so a stop signal works even while it is
    still waiting for the broker to answer.

    Raises:
        Exception: Whatever ``Agent.start`` raised, if it failed.

    """
    stopping = pynet.loop.Event()
    hooks = SignalHooks(stopping.set, lambda: None)
    hooks.install()
    starting = pynet.loop.spawn(agent.start(), name=f"agent-{agent.name}-start")

    def started(task: Future[None]) -> None:
        """Log a clean start; stop at once if the start failed."""
        if task.cancelled():
            return
        if task.exception() is not None:
            stopping.set()
        else:
            logger.info("agent %s is running", agent.name)

    starting.add_done_callback(started)
    try:
        await stopping.wait()
        if starting.done() and not starting.cancelled():
            starting.result()  # raise a failed start
    finally:
        starting.cancel()
        await agent.stop()
        hooks.restore()


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``pysched-agent`` with ``argv`` (default ``sys.argv[1:]``) and return its exit code."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        agent = build_agent(argv, clock=SystemClock())
    except SystemExit as exc:  # --help (0) or a usage error (2)
        return exc.code if isinstance(exc.code, int) else USAGE
    except AgentUsageError as exc:
        sys.stderr.write(f"pysched-agent: {exc}\n")
        return USAGE
    try:
        pynet.loop.run(run_agent(agent))
    except KeyboardInterrupt:
        return INTERRUPTED
    except (PyDBError, BrokerError, OSError) as exc:
        sys.stderr.write(f"pysched-agent: {exc}\n")
        return FAILED
    return OK
