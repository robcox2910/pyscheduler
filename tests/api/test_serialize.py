"""Test the JSON shapes of the API and the log tail reader."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pyscheduler.api import runs as runs_module
from pyscheduler.api.runs import read_tail
from pyscheduler.api.serialize import (
    date_text,
    duration_text,
    event_json,
    forecast_json,
    instant,
    schedule_text,
)
from pyscheduler.engine.model import Event
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.forecast import ForecastEntry
from pyscheduler.schedule.specs import (
    AtSchedule,
    BusinessDaySchedule,
    CronSchedule,
    EverySchedule,
)

NOON = datetime(2026, 10, 19, 12, tzinfo=UTC)
TWO_LINES = 2


class TestScalars:
    """Translate instants, dates and durations."""

    def test_instant_is_utc_with_z(self) -> None:
        """Write any aware instant in UTC with a ``Z``; keep ``None``."""
        london = datetime(2026, 7, 1, 13, tzinfo=ZoneInfo("Europe/London"))
        assert instant(london) == "2026-07-01T12:00:00Z"
        assert instant(None) is None
        assert date_text(None) is None
        assert date_text(date(2026, 1, 2)) == "2026-01-02"

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (timedelta(0), "0s"),
            (timedelta(seconds=45), "45s"),
            (timedelta(hours=1, minutes=30), "1h30m"),
            (timedelta(days=2, seconds=5), "2d5s"),
        ],
    )
    def test_duration_text(self, value: timedelta, text: str) -> None:
        """Write durations the way configs do."""
        assert duration_text(value) == text


class TestScheduleText:
    """Describe each kind of schedule in a few words."""

    def test_each_kind(self) -> None:
        """Describe cron, at, every and business-day schedules with their filters."""
        cron = CronSchedule(CronExpr.parse("0 6 * * 1-5"))
        assert schedule_text(cron) == "cron 0 6 * * 1-5"
        at = AtSchedule((time(9), time(17, 30, 15)), days=frozenset({0, 4}))
        assert schedule_text(at) == "at 09:00, 17:30:15 on mon,fri"
        every = EverySchedule(timedelta(minutes=15), time(8), time(18), months=frozenset({1, 2}))
        assert schedule_text(every) == "every 15m between 08:00-18:00 in months 1,2"
        last = BusinessDaySchedule(-1, (time(18),))
        assert schedule_text(last) == "business day -1 at 18:00"


class TestRecords:
    """Shape events and forecast entries."""

    def test_event_detail_falls_back_to_text(self) -> None:
        """Keep a detail that is not JSON as text, and an empty one as ``{}``."""
        odd = event_json(Event(NOON, "engine", None, "note", "not json"))
        assert odd["detail"] == "not json"
        assert odd["at"] == "2026-10-19T12:00:00Z"
        assert event_json(Event(NOON, "engine", None, "note", ""))["detail"] == {}

    def test_forecast_entry(self) -> None:
        """Add the expected end when the duration is known, else ``null``."""
        known = ForecastEntry("a", date(2026, 10, 19), NOON, timedelta(minutes=2), None)
        shaped = forecast_json(known)
        assert shaped["expected_end"] == "2026-10-19T12:02:00Z"
        assert shaped["expected_duration_seconds"] == timedelta(minutes=2).total_seconds()
        unknown = forecast_json(ForecastEntry("b", date(2026, 10, 19), NOON, None, "a"))
        assert unknown["expected_end"] is None
        assert unknown["via"] == "a"


class TestReadTail:
    """Read the end of a log without reading all of it."""

    def test_missing_and_empty(self, tmp_path: Path) -> None:
        """Return ``None`` for a missing file and no lines for an empty one."""
        assert read_tail(tmp_path / "nope.log", 1) is None
        empty = tmp_path / "empty.log"
        empty.write_bytes(b"")
        assert read_tail(empty, 1) == ([], False)

    def test_small_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Step back several blocks; keep a final line with no newline; strip CR."""
        monkeypatch.setattr(runs_module, "BLOCK", 4)
        path = tmp_path / "out.log"
        path.write_bytes(b"first line\r\nsecond line\nthird")
        assert read_tail(path, TWO_LINES) == (["second line", "third"], True)
        assert read_tail(path, 10) == (["first line", "second line", "third"], False)

    def test_byte_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stop at the byte cap and drop the partial first line."""
        monkeypatch.setattr(runs_module, "BLOCK", 4)
        monkeypatch.setattr(runs_module, "MAX_TAIL_BYTES", 8)
        path = tmp_path / "out.log"
        path.write_bytes(b"aaaaaaaaaa\nbb\ncc\n")
        assert read_tail(path, 10) == (["bb", "cc"], True)
