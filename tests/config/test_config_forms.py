"""Tests for the ways schedules and dependencies can be written in a config."""

import textwrap
from collections.abc import Callable
from datetime import time, timedelta
from pathlib import Path

import pytest

from pyscheduler.config import Dependency, DepOn, JobDef, Problem, SourceRef, build_config
from pyscheduler.schedule.specs import (
    AtSchedule,
    BusinessDaySchedule,
    CronSchedule,
    EverySchedule,
)

type WriteYaml = Callable[[str, str], Path]

TWO_SCHEDULES = 2
TWO_PROBLEMS = 2
LINE_FIVE = 5
LINE_SIX = 6
LINE_SEVEN = 7
LINE_EIGHT = 8
BUSINESS_DAY_LAST = -1
QUARTER_ENDS = frozenset({3, 6, 9, 12})
RUN_DATE_OFFSET = -1


def _job(name: str, body: str) -> str:
    """Return a ``jobs:`` document with one job whose keys are ``body`` lines."""
    lines = "".join(f"    {line}\n" for line in textwrap.dedent(body).strip().splitlines())
    return f"jobs:\n  {name}:\n{lines}"


def _errors(path: Path) -> list[Problem]:
    """Build a config and return its problems."""
    _, problems = build_config(path, env={})
    return problems


def _ok(path: Path) -> JobDef:
    """Build a config, assert it has no problems and return job ``j``."""
    config, problems = build_config(path, env={})
    assert problems == []
    return config.jobs["j"]


class TestScheduleForms:
    """Each way of writing a schedule."""

    def _schedules(self, write_yaml: WriteYaml, schedule: str) -> JobDef:
        """Load a job whose ``schedule`` line is ``schedule``."""
        path = write_yaml("s.yaml", _job("j", f"command: x\nschedule: {schedule}"))
        return _ok(path)

    def test_cron(self, write_yaml: WriteYaml) -> None:
        """A string is a cron expression."""
        job = self._schedules(write_yaml, '"30 18 * * mon-fri"')
        assert isinstance(job.schedules[0], CronSchedule)
        assert not job.manual

    def test_list(self, write_yaml: WriteYaml) -> None:
        """A list holds several schedules, and may include ``manual``."""
        job = self._schedules(write_yaml, '["@daily", {at: "09:00"}, manual]')
        assert len(job.schedules) == TWO_SCHEDULES
        assert job.manual

    def test_every(self, write_yaml: WriteYaml) -> None:
        """``every`` repeats within a window."""
        job = self._schedules(write_yaml, '{every: 15m, between: "08:00-18:00", days: [mon]}')
        assert job.schedules == (
            EverySchedule(timedelta(minutes=15), time(8), time(18), days=frozenset({0})),
        )

    def test_at(self, write_yaml: WriteYaml) -> None:
        """``at`` lists fixed times."""
        job = self._schedules(write_yaml, '{at: ["09:00", "17:30"]}')
        assert job.schedules == (AtSchedule((time(9), time(17, 30))),)

    def test_business_day(self, write_yaml: WriteYaml) -> None:
        """``business_day`` picks the n-th business day."""
        job = self._schedules(write_yaml, '{business_day: -1, at: "18:00", months: [3,6,9,12]}')
        assert job.schedules == (
            BusinessDaySchedule(BUSINESS_DAY_LAST, (time(18),), months=QUARTER_ENDS),
        )

    def test_manual(self, write_yaml: WriteYaml) -> None:
        """``manual`` means by hand only."""
        job = self._schedules(write_yaml, "manual")
        assert job.schedules == ()
        assert job.manual

    @pytest.mark.parametrize(
        ("schedule", "phrase"),
        [
            ('"61 * * * *"', "minute"),
            ("{every: 15m}", "between"),
            ('{every: 15m, between: "8-9"}', "HH:MM-HH:MM"),
            ('{business_day: 0, at: "07:00"}', "must not be 0"),
            ("{business_day: 1}", "needs at"),
            ("{days: [mon]}", "every, at or business_day"),
            ('{at: "09:00", months: [13]}', "months"),
            ('{at: "09:00", evry: 1m}', "did you mean 'every'"),
        ],
    )
    def test_bad_schedules(self, write_yaml: WriteYaml, schedule: str, phrase: str) -> None:
        """Bad schedules are problems with a helpful message."""
        path = write_yaml("s.yaml", _job("j", f"command: x\nschedule: {schedule}"))
        problems = _errors(path)
        assert len(problems) == 1
        assert phrase in problems[0].message


class TestDependencies:
    """Each way of writing a dependency."""

    def test_dependency_forms(self, write_yaml: WriteYaml) -> None:
        """The four forms from the spec's example."""
        body = """
        command: x
        depends_on:
          - extract-trades
          - {job: reconcile, on: done}
          - {job: load-warehouse, on: failure}
          - {job: close-of-day, run_date: -1}
        """
        path = write_yaml("s.yaml", _job("j", body))
        deps = _ok(path).depends_on
        file = str(path)
        assert deps == (
            Dependency("extract-trades", source=SourceRef(file, LINE_FIVE)),
            Dependency("reconcile", DepOn.DONE, source=SourceRef(file, LINE_SIX)),
            Dependency("load-warehouse", DepOn.FAILURE, source=SourceRef(file, LINE_SEVEN)),
            Dependency(
                "close-of-day", run_date_offset=RUN_DATE_OFFSET, source=SourceRef(file, LINE_EIGHT)
            ),
        )

    def test_bad_dependency(self, write_yaml: WriteYaml) -> None:
        """A dependency mapping needs ``job`` and a known ``on``."""
        body = "command: x\ndepends_on: [{on: done}, {job: a, on: maybe}]"
        problems = _errors(write_yaml("s.yaml", _job("j", body)))
        assert len(problems) == TWO_PROBLEMS
        assert "job" in problems[0].message
        assert "maybe" in problems[1].message
