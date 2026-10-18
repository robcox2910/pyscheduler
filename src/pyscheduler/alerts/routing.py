"""Decide who hears about an alert, and what the message looks like.

Every alert has a topic such as ``alert.failed.critical.load``. A route
is a pattern for topics (``alert.*.critical.#``), like a mailing list
that takes every message with a matching subject.
"""

from datetime import UTC

from pymq.broker.topics import topic_matches
from pynet.messaging import JsonValue

from pyscheduler.alerts.model import SCHEDULER_WORD, AlertRecord
from pyscheduler.config import Config


def alert_topic(alert: AlertRecord) -> str:
    """Return ``alert.<kind>.<criticality>.<job>`` (``_scheduler`` if no job)."""
    job = alert.job or SCHEDULER_WORD
    return f"alert.{alert.kind}.{alert.criticality.name.lower()}.{job}"


def destinations(config: Config, alert: AlertRecord) -> tuple[str, ...]:
    """Return the notifiers to tell, sorted and unique.

    That is every route whose pattern fits the topic, plus the job's own
    ``alerts.notify`` when the alert has a job that is still configured.
    """
    topic = alert_topic(alert)
    names = {
        n for route in config.routes if topic_matches(route.match, topic) for n in route.notify
    }
    job = config.jobs.get(alert.job) if alert.job is not None else None
    if job is not None:
        names.update(job.alerts.notify)
    return tuple(sorted(names))


def alert_json(alert: AlertRecord, *, scheduler: str) -> dict[str, JsonValue]:
    """Return the alert as the JSON object notifiers send."""
    raised = alert.raised_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "alert_id": alert.alert_id,
        "kind": alert.kind.value,
        "criticality": alert.criticality.name.lower(),
        "job": alert.job,
        "run_id": alert.run_id,
        "run_date": None if alert.run_date is None else alert.run_date.isoformat(),
        "attempt": alert.attempt,
        "raised_at": raised,
        "message": alert.message,
        "scheduler": scheduler,
        "detail": dict(alert.detail),
    }
