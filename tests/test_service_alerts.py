"""Tests for alerts in a running ``serve``: delivery, the API and a failed SIGHUP reload."""

import json
import os
import signal
from pathlib import Path
from typing import Any

from pyscheduler.client import ApiClient
from service_helpers import today, wait_until, with_service, write_config

JOBS = """
notifiers:
  rec: {type: log, path: alerts.log}
routes:
  - {match: "alert.#", notify: [rec]}
jobs:
  boom:
    command: "exit 1"
    schedule: manual
"""

BROKEN = "scheduler:\n  max_running: lots\n"


def read_log(path: Path) -> list[dict[str, Any]]:
    """Return the JSON lines of the alert log (none if it does not exist yet)."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestAlertsWhileServing:
    """``serve`` wires the dispatcher and the monitor to the engine."""

    def test_service_alerts_end_to_end(self, tmp_path: Path) -> None:
        """A failed job writes one line to the log, and the API shows it as sent."""
        path = write_config(tmp_path, JOBS)
        log = tmp_path / "alerts.log"

        def body(url: str) -> list[dict[str, Any]]:
            """Trigger ``boom`` and wait until its alert is sent."""
            client = ApiClient(url, None)
            client.trigger("boom", run_date=today())

            def sent() -> list[dict[str, Any]] | None:
                """Return the alerts once their delivery is sent."""
                found = client.alerts()["alerts"]
                done = found and all(d["status"] == "sent" for a in found for d in a["deliveries"])
                return found if done else None

            return wait_until(sent, "a sent alert")

        alerts = with_service(path, body)
        [line] = read_log(log)
        assert line["kind"] == "failed"
        assert line["job"] == "boom"
        assert [a["kind"] for a in alerts] == ["failed"]
        assert [d["notifier"] for d in alerts[0]["deliveries"]] == ["rec"]

    def test_sighup_reload_failure_alerts(self, tmp_path: Path) -> None:
        """A SIGHUP reload that finds errors raises a ``reload_failed`` alert."""
        path = write_config(tmp_path, JOBS)
        log = tmp_path / "alerts.log"

        def body(url: str) -> None:
            """Break the file, send SIGHUP, and wait for the alert line."""
            del url
            path.write_text(path.read_text(encoding="utf-8") + BROKEN, encoding="utf-8")
            os.kill(os.getpid(), signal.SIGHUP)
            wait_until(
                lambda: [x for x in read_log(log) if x["kind"] == "reload_failed"],
                "a reload_failed line",
            )

        with_service(path, body)
        [line] = [x for x in read_log(log) if x["kind"] == "reload_failed"]
        assert line["criticality"] == "high"
        assert line["job"] is None
