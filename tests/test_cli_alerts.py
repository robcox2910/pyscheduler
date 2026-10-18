"""Tests for ``pysched alerts`` and ``pysched replay`` against a live in-process server."""

from pathlib import Path

import pytest

from pyscheduler.cli import main
from pyscheduler.cli_alerts import worst_status
from pyscheduler.client import ApiClient
from service_helpers import today, wait_until, with_service, write_config

OK = 0
SERVER_ERROR = 1

JOBS = """
notifiers:
  pager:
    type: command
    command: "exit 1"
    retries: {count: 0}
routes:
  - {match: "alert.#", notify: [pager]}
jobs:
  boom:
    command: "exit 1"
    schedule: manual
"""


class TestWorstStatus:
    """The STATUS column shows the worst delivery."""

    def test_dead_beats_pending_beats_sent(self) -> None:
        """Pick by severity; no deliveries is ``none``."""
        sent, pending, dead = {"status": "sent"}, {"status": "pending"}, {"status": "dead"}
        assert worst_status([]) == "none"
        assert worst_status([sent]) == "sent"
        assert worst_status([sent, pending]) == "pending"
        assert worst_status([pending, dead, sent]) == "dead"


class TestAlertCommands:
    """The commands print the table and replay a dead delivery."""

    def test_cli_alerts_and_replay(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed command notifier leaves a dead delivery; ``replay`` exits 0 and it is tried again."""
        path = write_config(tmp_path, JOBS)

        def body(url: str) -> tuple[int, int, int, str]:
            """Trigger ``boom``, wait for the dead delivery, list, then replay it."""
            client = ApiClient(url, None)
            client.trigger("boom", run_date=today())
            [dead] = wait_until(lambda: client.dead_alerts()["deliveries"], "a dead delivery")
            alert_id = dead["alert"]["alert_id"]
            listed = main(["alerts", "--url", url])
            listed_dead = main(["alerts", "--url", url, "--dead"])
            capsys.readouterr()
            replayed = main(["replay", "--url", url, alert_id, "--notifier", "pager"])
            out = capsys.readouterr().out
            wait_until(lambda: client.dead_alerts()["deliveries"], "the replay to die again")
            return listed, listed_dead, replayed, out

        listed, listed_dead, replayed, out = with_service(path, body)
        assert (listed, listed_dead, replayed) == (OK, OK, OK)
        assert out.split()[1] == "pager:"
        assert out.split()[2] in {"pending", "dead"}

    def test_alerts_table_columns(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The table has the documented columns and shows ``dead`` for the failed delivery."""
        path = write_config(tmp_path, JOBS)

        def body(url: str) -> str:
            """Trigger ``boom``, wait until dead, and print the alerts table."""
            client = ApiClient(url, None)
            client.trigger("boom", run_date=today())
            wait_until(lambda: client.dead_alerts()["deliveries"], "a dead delivery")
            capsys.readouterr()
            assert main(["alerts", "--url", url, "--kind", "failed", "--job", "boom"]) == OK
            return capsys.readouterr().out

        header, row = with_service(path, body).splitlines()
        assert header.split() == ["RAISED", "KIND", "CRIT", "JOB", "RUN", "STATUS", "MESSAGE"]
        assert row.split()[1:6] == ["failed", "normal", "boom", f"boom.{today()}.1", "dead"]

    def test_replay_without_dead_delivery_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unknown alert is reported with exit code 1."""
        path = write_config(tmp_path, JOBS)

        def body(url: str) -> int:
            """Replay an alert that does not exist."""
            return main(["replay", "--url", url, "nope"])

        assert with_service(path, body) == SERVER_ERROR
        assert "unknown alert" in capsys.readouterr().err
