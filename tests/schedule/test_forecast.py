"""Tests for the schedule forecast."""

import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from pyscheduler.config import Config, load_config
from pyscheduler.schedule.forecast import ForecastEntry, forecast

MONDAY = date(2026, 10, 19)
START = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
END = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
TEN, TWENTY, THIRTY = 10 * MINUTE, 20 * MINUTE, 30 * MINUTE
FIRST_COUNT = 10
EXPECTED_COUNT = 3

HEADER = """\
calendars:
  uk-business:
    weekdays: [mon, tue, wed, thu, fri]
jobs:
"""


def make_config(tmp_path: Path, jobs: str) -> Config:
    """Load a config made of the header and the given jobs."""
    body = textwrap.dedent(jobs).strip("\n")
    text = HEADER + "".join(f"  {line}\n" for line in body.splitlines())
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path, env={}, today=MONDAY)


def at(hour: int, minute: int = 0, day: int = 19) -> datetime:
    """Return a UTC instant on the given October 2026 day."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


CHAIN = """
    a:
      command: x
      schedule: "30 18 * * *"
    b:
      command: x
      depends_on: [a]
"""


def by_job(entries: list[ForecastEntry], job: str) -> list[ForecastEntry]:
    """Return the entries of one job."""
    return [e for e in entries if e.job == job]


class TestForecast:
    """Forecasting scheduled and dependency-only jobs."""

    def test_chain_expected_start(self, tmp_path: Path) -> None:
        """A follower starts when its upstream is expected to end (median duration)."""
        config = make_config(tmp_path, CHAIN)
        entries = forecast(config, START, END, durations={"a": [TEN, TWENTY, THIRTY]})
        (b,) = by_job(entries, "b")
        assert b.expected_start == at(18, 50)
        assert b.via == "a"
        assert b.run_date == MONDAY
        (a,) = by_job(entries, "a")
        assert a.expected_start == at(18, 30)
        assert a.expected_duration == TWENTY
        assert a.via is None

    def test_unknown_duration(self, tmp_path: Path) -> None:
        """With no durations the follower starts at the upstream's start."""
        entries = forecast(make_config(tmp_path, CHAIN), START, END)
        (b,) = by_job(entries, "b")
        assert b.expected_start == at(18, 30)
        assert b.expected_duration is None

    def test_window_bounds(self, tmp_path: Path) -> None:
        """Start is exclusive and end inclusive."""
        config = make_config(tmp_path, CHAIN)
        assert by_job(forecast(config, at(18, 30), END), "a") == []
        assert len(by_job(forecast(config, START, at(18, 30)), "a")) == 1
        assert by_job(forecast(config, START, at(18, 29)), "a") == []

    def test_sorted_and_skips_manual_and_disabled(self, tmp_path: Path) -> None:
        """Entries sort by start then job; manual and disabled jobs are left out."""
        config = make_config(
            tmp_path,
            """
            zed:
              command: x
              schedule: "0 9 * * *"
            amy:
              command: x
              schedule: "0 9 * * *"
            early:
              command: x
              schedule: "0 8 * * *"
            hand:
              command: x
              schedule: manual
            off:
              command: x
              schedule: "0 7 * * *"
              enabled: false
            """,
        )
        entries = forecast(config, START, END)
        assert [e.job for e in entries] == ["early", "amy", "zed"]

    def test_median_uses_last_ten_and_even_average(self, tmp_path: Path) -> None:
        """Only the newest ten count, and an even count averages the middles."""
        config = make_config(tmp_path, CHAIN)
        newest_first = [TEN] * FIRST_COUNT + [timedelta(hours=9)] * 20
        (a,) = by_job(forecast(config, START, END, durations={"a": newest_first}), "a")
        assert a.expected_duration == TEN
        (a,) = by_job(forecast(config, START, END, durations={"a": [TEN, THIRTY]}), "a")
        assert a.expected_duration == TWENTY

    def test_dependency_only_chain(self, tmp_path: Path) -> None:
        """A chain of dependency-only jobs accumulates durations, whatever the file order."""
        config = make_config(
            tmp_path,
            """
            c:
              command: x
              depends_on: [b]
            b:
              command: x
              depends_on: [a]
            a:
              command: x
              schedule: "0 6 * * *"
            """,
        )
        durations = {"a": [TEN], "b": [TWENTY]}
        entries = forecast(config, START, END, durations=durations)
        (c,) = by_job(entries, "c")
        assert c.expected_start == at(6, 30)
        assert c.via == "b"

    def test_latest_upstream_wins_and_ties_go_to_first(self, tmp_path: Path) -> None:
        """The slowest upstream decides; equal upstreams pick the first in config order."""
        config = make_config(
            tmp_path,
            """
            fast:
              command: x
              schedule: "0 6 * * *"
            slow:
              command: x
              schedule: "0 7 * * *"
            tie:
              command: x
              schedule: "0 7 * * *"
            join:
              command: x
              depends_on: [fast, slow]
            tied:
              command: x
              depends_on: [tie, slow]
            """,
        )
        entries = forecast(config, START, END)
        (join,) = by_job(entries, "join")
        assert (join.expected_start, join.via) == (at(7), "slow")
        (tied,) = by_job(entries, "tied")
        assert tied.via == "tie"

    def test_any_upstream_creates_the_run(self, tmp_path: Path) -> None:
        """A follower appears when only one of its upstreams has an entry."""
        config = make_config(
            tmp_path,
            """
            a:
              command: x
              schedule: "0 6 * * *"
            m:
              command: x
              schedule: manual
            join:
              command: x
              depends_on: [a, m]
            """,
        )
        (join,) = by_job(forecast(config, START, END), "join")
        assert join.via == "a"

    def test_beyond_end_is_kept(self, tmp_path: Path) -> None:
        """A follower whose upstream is in the window stays even if it starts after end."""
        config = make_config(tmp_path, CHAIN)
        entries = forecast(config, START, at(18, 30), durations={"a": [TWENTY]})
        (b,) = by_job(entries, "b")
        assert b.expected_start == at(18, 50)

    def test_run_date_offset(self, tmp_path: Path) -> None:
        """An offset follower looks at the upstream run date shifted on its calendar."""
        config = make_config(
            tmp_path,
            """
            a:
              command: x
              schedule: "0 6 * * *"
              calendar: uk-business
            b:
              command: x
              calendar: uk-business
              depends_on:
                - {job: a, run_date: -1}
            """,
        )
        friday = datetime(2026, 10, 23, 12, tzinfo=UTC)
        entries = forecast(config, friday, datetime(2026, 10, 27, 12, tzinfo=UTC))
        b_dates = [e.run_date for e in by_job(entries, "b")]
        # a runs Mon 26 and Tue 27; b for run date R reads a at the day before R
        assert b_dates == [date(2026, 10, 27), date(2026, 10, 28)]
        assert [e.expected_start for e in by_job(entries, "b")] == [
            datetime(2026, 10, 26, 6, tzinfo=UTC),
            datetime(2026, 10, 27, 6, tzinfo=UTC),
        ]

    def test_cycle_is_skipped(self, tmp_path: Path) -> None:
        """Jobs in a dependency cycle (through offsets) produce no entries and no crash."""
        config = make_config(
            tmp_path,
            """
            a:
              command: x
              depends_on:
                - {job: b, run_date: -1}
            b:
              command: x
              depends_on: [a]
            """,
        )
        assert forecast(config, START, END) == []

    def test_naive_datetimes_rejected(self, tmp_path: Path) -> None:
        """Start and end must be timezone-aware."""
        config = make_config(tmp_path, CHAIN)
        with pytest.raises(ValueError, match="start"):
            forecast(config, datetime(2026, 10, 19), END)  # noqa: DTZ001
        with pytest.raises(ValueError, match="end"):
            forecast(config, START, datetime(2026, 10, 20))  # noqa: DTZ001

    def test_several_firings(self, tmp_path: Path) -> None:
        """Every firing in the window gets an entry; a follower appears once per run date."""
        config = make_config(
            tmp_path,
            """
            a:
              command: x
              schedule: "0 6,12,18 * * *"
            b:
              command: x
              depends_on: [a]
            """,
        )
        entries = forecast(config, START, END)
        assert len(by_job(entries, "a")) == EXPECTED_COUNT
        (b,) = by_job(entries, "b")
        assert b.expected_start == at(18)
