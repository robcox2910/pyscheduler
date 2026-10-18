"""Tests for alert topics, destinations and the JSON an alert is sent as."""

from pathlib import Path

from alert_helpers import RAISED, make_alert, make_config

from pyscheduler.alerts.model import SCHEDULER_WORD, alert_id_for
from pyscheduler.alerts.routing import alert_json, alert_topic, destinations
from pyscheduler.config.model import AlertKind, Criticality

SCHEDULER_NAME = "sched-1"


class TestTopic:
    """An alert's topic says what, how bad and which job."""

    def test_alert_topic(self) -> None:
        """Build the topic from kind, lowercase criticality and job."""
        assert alert_topic(make_alert()) == "alert.failed.critical.load"
        reload = make_alert(
            AlertKind.RELOAD_FAILED, run_id=None, job=None, criticality=Criticality.HIGH
        )
        assert alert_topic(reload) == f"alert.reload_failed.high.{SCHEDULER_WORD}"

    def test_alert_id(self) -> None:
        """Build the alert id from run, kind and attempt."""
        assert (
            alert_id_for("load.20261017.1", AlertKind.TOO_LONG, 2) == "load.20261017.1:too_long:2"
        )


class TestDestinations:
    """Routes and the job's own list are joined."""

    def test_destinations_union_sorted_unique(self, tmp_path: Path) -> None:
        """Join matching routes with the job's notifiers, once each, sorted."""
        config = make_config(tmp_path)
        assert destinations(config, make_alert()) == ("ops-slack", "team-log")
        normal = make_alert(AlertKind.TOO_LONG, criticality=Criticality.NORMAL)
        assert destinations(config, normal) == ("team-log",)

    def test_unknown_job_and_scheduler_alerts(self, tmp_path: Path) -> None:
        """Use routes only when the job is gone or the alert has no job."""
        config = make_config(tmp_path)
        gone = make_alert(job="removed")
        assert destinations(config, gone) == ("ops-slack", "team-log")
        quiet = make_alert(
            AlertKind.RELOAD_FAILED, run_id=None, job=None, criticality=Criticality.HIGH
        )
        assert destinations(config, quiet) == ()


class TestJson:
    """The JSON form is what notifiers send."""

    def test_alert_json_fields(self) -> None:
        """Show every field, with a Z instant and a lowercase criticality."""
        alert = make_alert()
        assert alert_json(alert, scheduler=SCHEDULER_NAME) == {
            "alert_id": "load.20261017.1:failed:1",
            "kind": "failed",
            "criticality": "critical",
            "job": "load",
            "run_id": "load.20261017.1",
            "run_date": "2026-10-17",
            "attempt": 1,
            "raised_at": "2026-10-17T09:30:00Z",
            "message": "load failed",
            "scheduler": SCHEDULER_NAME,
            "detail": {"exit_code": 2, "files": ["a", "b"]},
        }
        assert alert.raised_at == RAISED

    def test_runless_alert_json(self) -> None:
        """Show null for the job, run and date of a scheduler-wide alert."""
        alert = make_alert(AlertKind.RELOAD_FAILED, run_id=None, job=None)
        data = alert_json(alert, scheduler=SCHEDULER_NAME)
        assert (data["job"], data["run_id"], data["run_date"]) == (None, None, None)
