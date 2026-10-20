"""Names and payload shapes for the letters between scheduler and agents.

Every letter has a *type* (what it is about) and travels in one of two
mailboxes: each agent has its own ``agent.<name>.work`` box for orders
from the scheduler, and all agents post their news into the one shared
``scheduler.reports`` box.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pymq.broker import QueueConfig
from pynet.messaging import JsonValue

from pyscheduler.agents.signing import MAX_SKEW, NONCE_TTL, Rejected
from pyscheduler.engine.model import parse_run_id
from pyscheduler.engine.status_note import AttemptResult
from pyscheduler.values import check_name

__all__ = [
    "ACK_TYPE",
    "FINISHED_TYPE",
    "HEARTBEAT_EVERY",
    "HEARTBEAT_TYPE",
    "HELLO_TYPE",
    "KILL_SIGNALS",
    "KILL_TYPE",
    "LOG_REPLY_TYPE",
    "LOG_REQUEST_TYPE",
    "MAX_SKEW",
    "NONCE_TTL",
    "QUEUE_CONFIG",
    "REPORTS_QUEUE",
    "REPORT_TYPES",
    "SCHEDULER_TYPES",
    "STARTED_TYPE",
    "WORK_TYPE",
    "RunKey",
    "WorkOrder",
    "count_of",
    "finished_payload",
    "heartbeat_payload",
    "hello_payload",
    "key_payload",
    "kill_payload",
    "message_id",
    "parse_finished",
    "parse_heartbeat",
    "parse_hello",
    "parse_key",
    "parse_kill",
    "parse_started",
    "parse_work",
    "started_payload",
    "text_of",
    "work_payload",
    "work_queue",
]

type RunKey = tuple[str, int]
"""One attempt: ``(run_id, attempt)``."""

REPORTS_QUEUE = "scheduler.reports"
"""The shared box where agents post their reports."""
HELLO_TYPE = "hello"
STARTED_TYPE = "started"
HEARTBEAT_TYPE = "heartbeat"
FINISHED_TYPE = "finished"
LOG_REPLY_TYPE = "log_reply"
REPORT_TYPES = frozenset({HELLO_TYPE, STARTED_TYPE, HEARTBEAT_TYPE, FINISHED_TYPE, LOG_REPLY_TYPE})
"""The types an agent sends to the scheduler."""
WORK_TYPE = "work"
KILL_TYPE = "kill"
ACK_TYPE = "finished_ack"
LOG_REQUEST_TYPE = "log_request"
SCHEDULER_TYPES = frozenset({WORK_TYPE, KILL_TYPE, ACK_TYPE, LOG_REQUEST_TYPE})
"""The types the scheduler sends to an agent."""
HEARTBEAT_EVERY = timedelta(seconds=10)
"""How often an online agent says it is still there."""
KILL_SIGNALS = ("TERM", "KILL")
MALFORMED_KILL = "malformed kill"
MALFORMED_PAYLOAD = "malformed payload"
QUEUE_CONFIG = QueueConfig(max_attempts=1000, ack_timeout=30.0)
"""How both sides declare every agent queue (P4-R3): a dropped line counts as a nack."""


def work_queue(agent: str) -> str:
    """Return the name of ``agent``'s work queue."""
    return f"agent.{agent}.work"


def message_id(run_id: str, attempt: int) -> str:
    """Return the broker message id for one dispatch, so a re-send is deduplicated."""
    return f"{run_id}.{attempt}"


def kill_payload(run_id: str, attempt: int, signal: str) -> dict[str, JsonValue]:
    """Return the payload of a ``kill`` message; ``signal`` is ``TERM`` or ``KILL``."""
    if signal not in KILL_SIGNALS:
        msg = f"signal must be one of {KILL_SIGNALS}"
        raise ValueError(msg)
    return {"run_id": run_id, "attempt": attempt, "signal": signal}


def parse_kill(payload: Mapping[str, JsonValue]) -> tuple[str, int, str]:
    """Return ``(run_id, attempt, signal)`` from a kill payload or raise ``Rejected``."""
    run_id, attempt, signal = (payload.get(k) for k in ("run_id", "attempt", "signal"))
    if (
        not isinstance(run_id, str)
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or signal not in KILL_SIGNALS
    ):
        raise Rejected(MALFORMED_KILL)
    return run_id, attempt, str(signal)


def text_of(payload: Mapping[str, JsonValue], key: str) -> str:
    """Return the string ``key`` of ``payload`` or raise ``Rejected``."""
    value = payload.get(key)
    if not isinstance(value, str):
        raise Rejected(MALFORMED_PAYLOAD)
    return value


def count_of(payload: Mapping[str, JsonValue], key: str, *, low: int = 0) -> int:
    """Return the integer ``key`` (at least ``low``, never a bool) or raise ``Rejected``."""
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise Rejected(MALFORMED_PAYLOAD)
    return value


def _maybe(payload: Mapping[str, JsonValue], key: str, kind: type[int] | type[str]) -> JsonValue:
    """Return ``key`` if it is ``None`` or of ``kind`` (never a bool), else raise ``Rejected``."""
    value = payload.get(key)
    if value is not None and (not isinstance(value, kind) or isinstance(value, bool)):
        raise Rejected(MALFORMED_PAYLOAD)
    return value


def key_payload(run_id: str, attempt: int) -> dict[str, JsonValue]:
    """Return the payload naming one attempt (the whole of a ``finished_ack``)."""
    return {"run_id": run_id, "attempt": attempt}


def parse_key(payload: Mapping[str, JsonValue]) -> RunKey:
    """Return ``(run_id, attempt)`` from ``payload``; the run id must be a real one.

    A run id names files on the agent, so only ``<job>.<date>.<seq>`` with
    a job that passes the job-name rule (no ``/``, no ``..``) is accepted.
    """
    return _run_id(text_of(payload, "run_id"))[0], count_of(payload, "attempt", low=1)


def _run_id(run_id: str) -> tuple[str, str, str]:
    """Return ``(run_id, job, run_date)`` for a safe run id or raise ``Rejected``."""
    try:
        job, run_date, _ = parse_run_id(run_id)
        check_name(job)
    except ValueError:
        raise Rejected(MALFORMED_PAYLOAD) from None
    return run_id, job, run_date.isoformat()


def _keys(value: JsonValue) -> set[RunKey]:
    """Return the attempts in a wire list of ``[run_id, attempt]`` pairs."""
    if not isinstance(value, list):
        raise Rejected(MALFORMED_PAYLOAD)
    keys: set[RunKey] = set()
    for item in value:
        if not isinstance(item, list) or len(item) != len(("run_id", "attempt")):
            raise Rejected(MALFORMED_PAYLOAD)
        keys.add(parse_key({"run_id": item[0], "attempt": item[1]}))
    return keys


def _key_list(running: Iterable[RunKey]) -> list[JsonValue]:
    """Return ``running`` as a sorted wire list of ``[run_id, attempt]`` pairs."""
    return [[run_id, attempt] for run_id, attempt in sorted(running)]


@dataclass(frozen=True)
class WorkOrder:
    """One attempt the scheduler asks an agent to run (P4-R6).

    The command and environment may hold operators' secrets, so neither
    is shown in the repr.
    """

    run_id: str
    attempt: int
    job: str
    run_date: str
    argv: tuple[str, ...] = field(repr=False)
    cwd: str | None
    env: Mapping[str, str] = field(repr=False)

    @property
    def key(self) -> RunKey:
        """Return ``(run_id, attempt)``."""
        return self.run_id, self.attempt


def work_payload(order: WorkOrder) -> dict[str, JsonValue]:
    """Return the payload of a ``work`` message."""
    return {
        **key_payload(order.run_id, order.attempt),
        "job": order.job,
        "run_date": order.run_date,
        "argv": list(order.argv),
        "cwd": order.cwd,
        "env": dict(order.env),
    }


def parse_work(payload: Mapping[str, JsonValue]) -> WorkOrder:
    """Return the ``WorkOrder`` in a work payload or raise ``Rejected``.

    The job and run date name folders on the agent, so they must be the
    ones spelled in the run id (which passes the job-name rule); the
    command must be a non-empty list of text.
    """
    run_id, attempt = parse_key(payload)
    _, job, run_date = _run_id(run_id)
    if (text_of(payload, "job"), text_of(payload, "run_date")) != (job, run_date):
        raise Rejected(MALFORMED_PAYLOAD)
    argv, env = payload.get("argv"), payload.get("env")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise Rejected(MALFORMED_PAYLOAD)
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise Rejected(MALFORMED_PAYLOAD)
    cwd = _maybe(payload, "cwd", str)
    words = tuple(str(a) for a in argv)
    variables = {k: str(v) for k, v in env.items()}
    return WorkOrder(
        run_id, attempt, job, run_date, words, cwd if isinstance(cwd, str) else None, variables
    )


def hello_payload(
    tags: Iterable[str], max_running: int, running: Iterable[RunKey]
) -> dict[str, JsonValue]:
    """Return the payload of a ``hello``: the agent's tags, capacity and current attempts."""
    names: list[JsonValue] = [*sorted(tags)]
    return {
        "tags": names,
        "max_running": max_running,
        "running": _key_list(running),
    }


def parse_hello(payload: Mapping[str, JsonValue]) -> tuple[frozenset[str], int, set[RunKey]]:
    """Return ``(tags, max_running, running)`` from a hello payload or raise ``Rejected``."""
    tags = payload.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise Rejected(MALFORMED_PAYLOAD)
    names = frozenset(str(t) for t in tags)
    try:
        for name in names:
            check_name(name)
    except ValueError:
        raise Rejected(MALFORMED_PAYLOAD) from None
    return names, count_of(payload, "max_running"), _keys(payload.get("running"))


def heartbeat_payload(running: Iterable[RunKey]) -> dict[str, JsonValue]:
    """Return the payload of a ``heartbeat``: the attempts running right now."""
    return {"running": _key_list(running)}


def parse_heartbeat(payload: Mapping[str, JsonValue]) -> set[RunKey]:
    """Return the running attempts from a heartbeat payload or raise ``Rejected``."""
    return _keys(payload.get("running"))


def started_payload(
    run_id: str, attempt: int, stdout_path: str, stderr_path: str
) -> dict[str, JsonValue]:
    """Return the payload of a ``started`` report, with the agent-side log paths."""
    return {**key_payload(run_id, attempt), "stdout_path": stdout_path, "stderr_path": stderr_path}


def parse_started(payload: Mapping[str, JsonValue]) -> tuple[RunKey, str, str]:
    """Return ``(key, stdout_path, stderr_path)`` from a started payload or raise ``Rejected``."""
    return parse_key(payload), text_of(payload, "stdout_path"), text_of(payload, "stderr_path")


def finished_payload(run_id: str, attempt: int, result: AttemptResult) -> dict[str, JsonValue]:
    """Return the payload of a ``finished`` report: the ``AttemptResult`` with ISO times."""
    started = result.started_at.isoformat() if result.started_at is not None else None
    return {
        **key_payload(run_id, attempt),
        "exit_code": result.exit_code,
        "signal": result.signal,
        "started_at": started,
        "ended_at": result.ended_at.isoformat(),
        "start_error": result.start_error,
    }


def _instant(text: JsonValue) -> datetime:
    """Return the aware instant in ``text`` or raise ``Rejected``."""
    if not isinstance(text, str):
        raise Rejected(MALFORMED_PAYLOAD)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise Rejected(MALFORMED_PAYLOAD) from None
    if when.tzinfo is None:
        raise Rejected(MALFORMED_PAYLOAD)
    return when


def parse_finished(payload: Mapping[str, JsonValue]) -> tuple[RunKey, AttemptResult]:
    """Return ``(key, result)`` from a finished payload or raise ``Rejected``."""
    key = parse_key(payload)
    exit_code, sig = _maybe(payload, "exit_code", int), _maybe(payload, "signal", int)
    error = _maybe(payload, "start_error", str)
    began = payload.get("started_at")
    result = AttemptResult(
        exit_code=exit_code if isinstance(exit_code, int) else None,
        signal=sig if isinstance(sig, int) else None,
        started_at=None if began is None else _instant(began),
        ended_at=_instant(payload.get("ended_at")),
        start_error=error if isinstance(error, str) else None,
    )
    return key, result
