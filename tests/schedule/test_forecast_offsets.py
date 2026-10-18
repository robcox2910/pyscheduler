"""Forecast keeps jobs whose only loops go through run-date offsets."""

import textwrap
from datetime import UTC, date, datetime
from pathlib import Path

from pyscheduler.config import Config, load_config
from pyscheduler.schedule.forecast import forecast

MONDAY = date(2026, 10, 19)
START = datetime(2026, 10, 19, tzinfo=UTC)
END = datetime(2026, 10, 21, tzinfo=UTC)


def make_config(tmp_path: Path, jobs: str) -> Config:
    """Load a config of the given jobs (all on every day)."""
    body = textwrap.dedent(jobs).strip("\n")
    path = tmp_path / "scheduler.yaml"
    path.write_text("jobs:\n" + "".join(f"  {line}\n" for line in body.splitlines()))
    return load_config(path, env={}, today=MONDAY)


class TestOffsetLoops:
    """Only offset-0 edges order the followers."""

    def test_self_dependency_on_yesterday(self, tmp_path: Path) -> None:
        """``load`` needs ``extract`` and its own yesterday: it is forecast."""
        config = make_config(
            tmp_path,
            """
            extract: {command: x, schedule: "0 18 * * *"}
            load:
              command: y
              depends_on: [extract, {job: load, run_date: -1}]
            """,
        )
        loads = [e.run_date for e in forecast(config, START, END) if e.job == "load"]
        assert loads == [MONDAY, date(2026, 10, 20)]

    def test_loop_through_an_offset(self, tmp_path: Path) -> None:
        """``a`` needs yesterday's ``b``, ``b`` needs today's ``a``: both are forecast."""
        config = make_config(
            tmp_path,
            """
            x: {command: x, schedule: "0 18 * * *"}
            a:
              command: a
              depends_on: [x, {job: b, run_date: -1}]
            b:
              command: b
              depends_on: [a]
            """,
        )
        entries = forecast(config, START, END)
        assert {e.job for e in entries} == {"x", "a", "b"}
        b = [e for e in entries if e.job == "b"]
        assert [e.via for e in b] == ["a", "a"]
