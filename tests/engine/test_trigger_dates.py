"""``trigger`` without a run date follows the job's ``run_date`` rule (ruling R32)."""

from datetime import date, timedelta

from engine_helpers import START, MakeHarness

ALICE = "alice"
FRIDAY = date(2026, 10, 16)
SUNDAY_IN_NEW_YORK = date(2026, 10, 18)
SATURDAY = START + timedelta(days=5)

JOBS = """
calendars:
  uk: {weekdays: [mon, tue, wed, thu, fri]}
jobs:
  plain: {command: x, schedule: manual}
  prev: {command: x, schedule: manual, calendar: uk, run_date: previous_business_day}
  biz: {command: x, schedule: manual, calendar: uk}
  ny: {command: x, schedule: manual, timezone: America/New_York}
"""


class TestDefaultRunDate:
    """The default comes from the job's rule, calendar and time zone."""

    def test_monday(self, harness: MakeHarness) -> None:
        """On Monday: same_day is today; previous_business_day is Friday; zones count."""
        h = harness(JOBS)

        async def body() -> None:
            """Trigger each job with no run date."""
            assert (await h.engine.trigger("plain", actor=ALICE)).run_date == START.date()
            assert (await h.engine.trigger("prev", actor=ALICE)).run_date == FRIDAY
            assert (await h.engine.trigger("biz", actor=ALICE)).run_date == START.date()
            assert (await h.engine.trigger("ny", actor=ALICE)).run_date == SUNDAY_IN_NEW_YORK

        h.run_engine(body)

    def test_weekend_same_day_with_calendar(self, harness: MakeHarness) -> None:
        """On Saturday a same_day job with a calendar gets Friday; without one, Saturday."""
        h = harness(JOBS)

        async def body() -> None:
            """Move to Saturday and trigger."""
            await h.advance_to(SATURDAY)
            friday = SATURDAY.date() - timedelta(days=1)
            assert (await h.engine.trigger("biz", actor=ALICE)).run_date == friday
            assert (await h.engine.trigger("prev", actor=ALICE)).run_date == friday
            assert (await h.engine.trigger("plain", actor=ALICE)).run_date == SATURDAY.date()

        h.run_engine(body)
