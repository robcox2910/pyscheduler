"""The ``pysched alerts`` and ``pysched replay`` commands.

``alerts`` is the office noticeboard: one line per alert, with how its
delivery went. ``replay`` asks the post office to try again with the
copies that gave up.
"""

import argparse
from collections.abc import Callable, Mapping, Sequence

from pyscheduler.cli_commands import OK, say
from pyscheduler.cli_format import table
from pyscheduler.client import ApiClient

__all__ = ["ALERT_COMMANDS", "ALERT_HEADER", "DEAD_HEADER", "alerts", "replay", "worst_status"]

ALERT_HEADER = ("RAISED", "KIND", "CRIT", "JOB", "RUN", "STATUS", "MESSAGE")
"""The columns of the ``alerts`` table."""

DEAD_HEADER = ("ALERT", "NOTIFIER", "ATTEMPTS", "UPDATED", "ERROR")
"""The columns of the ``alerts --dead`` table."""

NO_DELIVERIES = "none"
"""The STATUS of an alert that has no deliveries (no notifier was routed to it)."""

_SEVERITY = {"sent": 0, "pending": 1, "dead": 2}
"""How bad a delivery status is: dead beats pending beats sent."""


def worst_status(deliveries: Sequence[Mapping[str, object]]) -> str:
    """Return the worst status of ``deliveries`` (``none`` when there are none)."""
    if not deliveries:
        return NO_DELIVERIES
    return max((str(d["status"]) for d in deliveries), key=lambda s: _SEVERITY.get(s, 0))


def _one_line(message: object) -> str:
    """Return the first line of ``message`` (an empty one stays empty)."""
    lines = str(message or "").splitlines()
    return lines[0] if lines else ""


def alerts(client: ApiClient, args: argparse.Namespace) -> int:
    """Print the alerts (``--kind``, ``--job``, ``--since``); ``--dead`` lists dead deliveries."""
    if args.dead:
        dead = client.dead_alerts()["deliveries"]
        rows: list[list[object]] = [
            [
                d["alert"]["alert_id"],
                d["notifier"],
                d["attempts"],
                d["updated_at"],
                _one_line(d["last_error"]),
            ]
            for d in dead
        ]
        say(table(DEAD_HEADER, rows))
        return OK
    found = client.alerts(kind=args.kind, job=args.job, since=args.since)["alerts"]
    rows = [
        [
            a["raised_at"],
            a["kind"],
            a["criticality"],
            a["job"],
            a["run_id"],
            worst_status(a["deliveries"]),
            _one_line(a["message"]),
        ]
        for a in found
    ]
    say(table(ALERT_HEADER, rows))
    return OK


def replay(client: ApiClient, args: argparse.Namespace) -> int:
    """Send an alert's dead deliveries again; print each as ``notifier: status``."""
    for delivery in client.replay(args.alert, notifier=args.notifier)["deliveries"]:
        say(f"{args.alert} {delivery['notifier']}: {delivery['status']}")
    return OK


ALERT_COMMANDS: dict[str, Callable[[ApiClient, argparse.Namespace], int]] = {
    "alerts": alerts,
    "replay": replay,
}
"""The alert commands, by name (added to the client commands)."""
