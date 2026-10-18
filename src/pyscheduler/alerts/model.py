"""The records for alerts and for each notifier's delivery of them."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from pynet.messaging import JsonValue

from pyscheduler.config.model import AlertKind, Criticality

SCHEDULER_WORD = "_scheduler"
"""The word that stands in a topic for the job of a scheduler-wide alert."""


class DeliveryStatus(StrEnum):
    """Where one notifier's copy of an alert has got to."""

    PENDING = "pending"
    SENT = "sent"
    DEAD = "dead"


@dataclass(frozen=True)
class AlertRecord:
    """One alert: what happened, to which run, how serious, and when.

    Scheduler-wide alerts (a failed reload) have no run, job or run date.
    """

    alert_id: str
    kind: AlertKind
    run_id: str | None
    job: str | None
    run_date: date | None
    attempt: int
    criticality: Criticality
    raised_at: datetime
    message: str
    detail: Mapping[str, JsonValue]


@dataclass(frozen=True)
class DeliveryRecord:
    """One notifier's copy of one alert, and how its sending is going."""

    alert_id: str
    notifier: str
    status: DeliveryStatus
    attempts: int
    last_error: str
    updated_at: datetime

    @property
    def delivery_id(self) -> str:
        """Return the id of this delivery: the alert and the notifier."""
        return f"{self.alert_id}|{self.notifier}"


def alert_id_for(run_id: str, kind: AlertKind, attempt: int) -> str:
    """Return the id of the alert of ``kind`` raised for ``attempt`` of a run."""
    return f"{run_id}:{kind}:{attempt}"
