"""Build alerts and a small config for the alert tests."""

import textwrap
from datetime import UTC, date, datetime
from pathlib import Path

from pyscheduler.alerts.model import AlertRecord, alert_id_for
from pyscheduler.config import Config, load_config
from pyscheduler.config.model import AlertKind, Criticality

RAISED = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
RUN_DATE = date(2026, 10, 17)

CONFIG = """\
notifiers:
  team-log: {type: log, path: logs/alerts.log}
  ops-slack: {type: slack, webhook_url: "https://hooks.example/abc"}
routes:
  - {match: "alert.*.critical.#", notify: [ops-slack]}
  - {match: "alert.failed.#", notify: [team-log]}
jobs:
  load:
    command: "true"
    schedule: manual
    alerts: {notify: [team-log]}
"""


def make_config(tmp_path: Path) -> Config:
    """Load the sample config (one job ``load`` that notifies ``team-log``)."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    return load_config(path, env={})


def make_alert(
    kind: AlertKind = AlertKind.FAILED,
    *,
    run_id: str | None = "load.20261017.1",
    job: str | None = "load",
    criticality: Criticality = Criticality.CRITICAL,
    attempt: int = 1,
    raised_at: datetime = RAISED,
) -> AlertRecord:
    """Return an alert; with ``run_id=None`` it is a run-less one."""
    alert_id = (
        alert_id_for(run_id, kind, attempt)
        if run_id is not None
        else f"_scheduler:{kind}:{raised_at:%Y%m%dT%H%M%S%fZ}"
    )
    return AlertRecord(
        alert_id=alert_id,
        kind=kind,
        run_id=run_id,
        job=job,
        run_date=None if run_id is None else RUN_DATE,
        attempt=attempt,
        criticality=criticality,
        raised_at=raised_at,
        message="load failed",
        detail={"exit_code": 2, "files": ["a", "b"]},
    )
