"""Names and payload shapes for the letters between scheduler and agents.

Every letter has a *type* (what it is about) and travels in one of two
mailboxes: each agent has its own ``agent.<name>.work`` box for orders
from the scheduler, and all agents post their news into the one shared
``scheduler.reports`` box.
"""

from collections.abc import Mapping
from datetime import timedelta

from pynet.messaging import JsonValue

from pyscheduler.agents.signing import MAX_SKEW, NONCE_TTL, Rejected

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
    "REPORTS_QUEUE",
    "REPORT_TYPES",
    "SCHEDULER_TYPES",
    "STARTED_TYPE",
    "WORK_TYPE",
    "kill_payload",
    "message_id",
    "parse_kill",
    "work_queue",
]

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
