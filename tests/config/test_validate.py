"""Tests for semantic config validation (the checks that need the whole config)."""

import textwrap
import time
from datetime import date
from pathlib import Path

import pytest

from pyscheduler.config import Config, ConfigError, Problem, build_config, load_config
from pyscheduler.config.validate import validate_config

TODAY = date(2026, 10, 18)
JOB_COUNT = 200
MAX_SECONDS = 2.0
FOUR_PROBLEMS = 4

HEADER = """\
calendars:
  uk-business:
    weekdays: [mon, tue, wed, thu, fri]
  us-business:
    weekdays: [mon, tue, wed, thu, fri]
pools:
  database: 2
jobs:
"""


def _text(jobs: str) -> str:
    """Return the header followed by ``jobs`` (dedented, indented under ``jobs:``)."""
    body = textwrap.dedent(jobs).strip("\n")
    return HEADER + "".join(f"  {line}\n" for line in body.splitlines())


def _line(text: str, needle: str) -> int:
    """Return the 1-based line of the first line of ``text`` containing ``needle``."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    msg = f"{needle!r} not in text"
    raise AssertionError(msg)


def _write(tmp_path: Path, text: str) -> Path:
    """Write ``text`` to ``scheduler.yaml`` under ``tmp_path``."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _errors(tmp_path: Path, text: str) -> tuple[Problem, ...]:
    """Load ``text`` and return the problems of the ``ConfigError`` it raises."""
    with pytest.raises(ConfigError) as info:
        load_config(_write(tmp_path, text), env={})
    return info.value.problems


def _warnings(tmp_path: Path, text: str) -> tuple[Problem, ...]:
    """Load ``text`` (which must have no errors) and return its warnings."""
    return load_config(_write(tmp_path, text), env={}).warnings


def _one(problems: tuple[Problem, ...], phrase: str) -> Problem:
    """Return the only problem whose message contains ``phrase``."""
    found = [p for p in problems if phrase in p.message]
    assert len(found) == 1, problems
    return found[0]


def _check(problem: Problem, job: str | None, line: int) -> None:
    """Assert the problem's job and line."""
    assert problem.job == job
    assert problem.line == line
    assert problem.file.endswith("scheduler.yaml")


class TestDependencyErrors:
    """Errors about ``depends_on``."""

    def test_unknown_job(self, tmp_path: Path) -> None:
        """A dependency on a job that does not exist is reported at its line."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - ghost
        """)
        problem = _one(_errors(tmp_path, text), "unknown job")
        _check(problem, "a", _line(text, "- ghost"))

    def test_cycle(self, tmp_path: Path) -> None:
        """An offset-0 cycle is reported once, listing the path."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - b
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - a
        """)
        problem = _one(_errors(tmp_path, text), "cycle")
        assert "cycle: a -> b -> a" in problem.message
        _check(problem, "a", _line(text, "- b"))

    def test_three_job_cycle_reported_once(self, tmp_path: Path) -> None:
        """A cycle through three jobs gives one problem, starting at the smallest name."""
        text = _text("""
            c:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on: [b]
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on: [c]
        """)
        problem = _one(_errors(tmp_path, text), "cycle")
        assert "cycle: a -> b -> c -> a" in problem.message

    def test_self_dependency_is_a_cycle(self, tmp_path: Path) -> None:
        """A job depending on itself for the same run date is a cycle."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
        """)
        assert "cycle: a -> a" in _one(_errors(tmp_path, text), "cycle").message

    def test_offset_cycle_allowed(self, tmp_path: Path) -> None:
        """A cycle broken by a -1 run-date offset is fine."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - {job: b, run_date: -1}
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
        """)
        config = load_config(_write(tmp_path, text), env={})
        assert set(config.jobs) == {"a", "b"}

    def test_positive_offset(self, tmp_path: Path) -> None:
        """A positive run-date offset is an error at the dependency's line."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - {job: b, run_date: 1}
            b:
              command: x
              schedule: "0 9 * * *"
        """)
        problem = _one(_errors(tmp_path, text), "positive run-date offset")
        _check(problem, "a", _line(text, "run_date: 1"))

    def test_different_calendars(self, tmp_path: Path) -> None:
        """A uk-business job depending on a us-business job is an error."""
        text = _text("""
            uk-job:
              command: x
              schedule: "0 9 * * *"
              calendar: uk-business
              depends_on:
                - us-job
            us-job:
              command: x
              schedule: "0 8 * * *"
              calendar: us-business
        """)
        problem = _one(_errors(tmp_path, text), "different calendars")
        _check(problem, "uk-job", _line(text, "- us-job"))

    def test_calendar_job_on_none_calendar_job(self, tmp_path: Path) -> None:
        """A calendar job depending on a job with no calendar is an error."""
        text = _text("""
            uk-job:
              command: x
              schedule: "0 9 * * *"
              calendar: uk-business
              depends_on:
                - plain
            plain:
              command: x
              schedule: "0 8 * * *"
        """)
        problem = _one(_errors(tmp_path, text), "calendar job depends on a job with no calendar")
        _check(problem, "uk-job", _line(text, "- plain"))

    def test_scheduled_none_calendar_job_on_calendar_job(self, tmp_path: Path) -> None:
        """A scheduled job with no calendar depending on a calendar job is an error."""
        text = _text("""
            plain:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - uk-job
            uk-job:
              command: x
              schedule: "0 8 * * *"
              calendar: uk-business
        """)
        problem = _one(_errors(tmp_path, text), "no calendar depends on a calendar job")
        _check(problem, "plain", _line(text, "- uk-job"))

    def test_dependency_only_none_calendar_job_may_follow_calendar_job(
        self, tmp_path: Path
    ) -> None:
        """A dependency-only job with no calendar takes its run dates from its upstream."""
        text = _text("""
            uk-job:
              command: x
              schedule: "0 8 * * *"
              calendar: uk-business
            follower:
              command: x
              depends_on: [uk-job]
        """)
        assert load_config(_write(tmp_path, text), env={}).warnings == ()


class TestScheduleErrors:
    """Errors about schedules and time zones."""

    def test_business_day_needs_calendar(self, tmp_path: Path) -> None:
        """A business_day schedule on a job with no calendar is an error."""
        text = _text("""
            a:
              command: x
              schedule: {business_day: 1, at: "07:00"}
        """)
        problem = _one(_errors(tmp_path, text), "business_day needs a calendar")
        _check(problem, "a", _line(text, "  a:"))

    def test_business_day_out_of_range(self, tmp_path: Path) -> None:
        """A business_day beyond any month's length is an error, found quickly."""
        text = _text("""
            a:
              command: x
              calendar: uk-business
              schedule: {business_day: 1000000000, at: "07:00"}
        """)
        problem = _one(_errors(tmp_path, text), "business_day must be between -31 and 31")
        _check(problem, "a", _line(text, "  a:"))

    def test_never_fires(self, tmp_path: Path) -> None:
        """A schedule for 30 February never fires."""
        text = _text("""
            a:
              command: x
              schedule: "0 0 30 2 *"
        """)
        problem = _one(_errors(tmp_path, text), "never fires")
        _check(problem, "a", _line(text, "  a:"))

    def test_empty_days_never_fires(self, tmp_path: Path) -> None:
        """An empty ``days`` filter never fires."""
        text = _text("""
            a:
              command: x
              schedule: {at: "07:00", days: []}
        """)
        _one(_errors(tmp_path, text), "never fires")

    def test_calendar_without_business_days(self, tmp_path: Path) -> None:
        """A calendar with no weekdays and no extra days is an error; its jobs are not scanned."""
        text = HEADER.replace("calendars:\n", "calendars:\n  empty:\n    weekdays: []\n") + (
            '  a:\n    command: x\n    calendar: empty\n    schedule: "0 9 * * *"\n'
        )
        problems = _errors(tmp_path, text)
        _check(_one(problems, "calendar 'empty' has no business days"), None, _line(text, "empty:"))
        assert not [p for p in problems if "never fires" in p.message]

    def test_unknown_time_zone(self, tmp_path: Path) -> None:
        """An unknown job time zone is reported (by the loader) at its line."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              timezone: Mars/Base
        """)
        _check(_one(_errors(tmp_path, text), "unknown time zone"), "a", _line(text, "Mars/Base"))


class TestResourceErrors:
    """Errors about calendars, pools and placeholders."""

    def test_unknown_calendar(self, tmp_path: Path) -> None:
        """A job naming an undefined calendar is an error."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              calendar: nope
        """)
        _check(_one(_errors(tmp_path, text), "unknown calendar"), "a", _line(text, "  a:"))

    def test_unknown_pool(self, tmp_path: Path) -> None:
        """A job naming an undefined pool is an error."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              pools: {nope: 1}
        """)
        _check(_one(_errors(tmp_path, text), "unknown pool"), "a", _line(text, "  a:"))

    def test_too_many_slots(self, tmp_path: Path) -> None:
        """A job needing more slots than the pool has is an error."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              pools: {database: 3}
        """)
        problem = _one(_errors(tmp_path, text), "needs 3 slots but pool 'database' has 2")
        _check(problem, "a", _line(text, "  a:"))

    def test_calendar_include_cycle(self, tmp_path: Path) -> None:
        """Calendars including each other are an error, reported once."""
        text = (
            "calendars:\n  x:\n    include: [y]\n  y:\n    include: [x]\n"
            'jobs:\n  a:\n    command: x\n    schedule: "0 9 * * *"\n'
        )
        problem = _one(_errors(tmp_path, text), "calendar include cycle")
        assert "x -> y -> x" in problem.message
        _check(problem, None, _line(text, "  x:"))

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("command", "run {rundate}"),
            ("cwd", "/work/{rundate}"),
            ("env", "{A: '{rundate}'}"),
            ("stdout", "/logs/{rundate}.out"),
            ("stderr", "/logs/{rundate}.err"),
        ],
    )
    def test_unknown_placeholder(self, tmp_path: Path, key: str, value: str) -> None:
        """An unknown placeholder is an error in every templated key."""
        written = value if key == "env" else f'"{value}"'
        text = _text('a:\n  schedule: "0 9 * * *"\n')
        if key != "command":
            text += "    command: x\n"
        text += f"    {key}: {written}\n"
        problem = _one(_errors(tmp_path, text), "unknown placeholder {rundate}")
        assert key in problem.message
        _check(problem, "a", _line(text, "  a:"))

    def test_command_list_placeholders(self, tmp_path: Path) -> None:
        """Each argument of a command list is checked; known placeholders pass."""
        text = _text("""
            a:
              command: [run, "{run_date:%Y%m%d}", "{oops}"]
              schedule: "0 9 * * *"
        """)
        _one(_errors(tmp_path, text), "unknown placeholder {oops}")


class TestJobErrors:
    """Errors about one job's own settings."""

    @pytest.mark.parametrize(
        ("keys", "phrase"),
        [
            ("min_runtime: 10m\nmax_runtime: 5m", "min_runtime must be less than max_runtime"),
            ("min_runtime: 5m\nmax_runtime: 5m", "min_runtime must be less than max_runtime"),
            ("max_runtime: 2h\ntimeout: 1h", "max_runtime exceeds timeout"),
            ("retries: {count: -1}", "negative retry count"),
            ("retries: {backoff: 0.5}", "backoff must be at least 1"),
            ("agent: box1", "agents arrive in phase 4"),
        ],
    )
    def test_job_setting(self, tmp_path: Path, keys: str, phrase: str) -> None:
        """Each bad job setting is reported at the job's line."""
        extra = "".join(f"    {line}\n" for line in keys.splitlines())
        text = _text('a:\n  command: x\n  schedule: "0 9 * * *"\n') + extra
        _check(_one(_errors(tmp_path, text), phrase), "a", _line(text, "  a:"))

    def test_no_way_to_run(self, tmp_path: Path) -> None:
        """A job with no schedule, no dependencies and not manual can never run."""
        text = _text("""
            a:
              command: x
        """)
        problem = _one(_errors(tmp_path, text), "no schedule, no dependencies and not manual")
        _check(problem, "a", _line(text, "  a:"))

    def test_manual_job_is_fine(self, tmp_path: Path) -> None:
        """A manual-only job needs nothing else."""
        text = _text("""
            a:
              command: x
              schedule: manual
        """)
        assert load_config(_write(tmp_path, text), env={}).warnings == ()

    def test_loader_errors_are_not_repeated(self, tmp_path: Path) -> None:
        """A job whose schedule failed to load does not also get "no schedule"."""
        text = _text("""
            a:
              command: x
              schedule: "bad cron"
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
        """)
        problems = _errors(tmp_path, text)
        assert len(problems) == 1
        assert "bad cron" in problems[0].message

    def test_all_reported_together(self, tmp_path: Path) -> None:
        """Four different errors come back in one ConfigError."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              calendar: nope
            b:
              command: x
            c:
              command: x
              schedule: "0 9 * * *"
              depends_on: [ghost]
            d:
              command: x
              schedule: "0 9 * * *"
              agent: box1
        """)
        problems = _errors(tmp_path, text)
        assert len(problems) == FOUR_PROBLEMS
        assert [p.job for p in problems] == ["a", "b", "c", "d"]


class TestWarnings:
    """Warnings that do not stop the load."""

    def test_would_wait_forever(self, tmp_path: Path) -> None:
        """A daily job depending on a Monday-to-Friday job waits forever at weekends."""
        text = _text("""
            downstream:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - upstream
            upstream:
              command: x
              schedule: "0 8 * * 1-5"
        """)
        problem = _one(_warnings(tmp_path, text), "would wait forever")
        assert problem.severity == "warning"
        _check(problem, "downstream", _line(text, "- upstream"))

    def test_matching_schedules_do_not_wait(self, tmp_path: Path) -> None:
        """Upstream and downstream on the same weekdays give no warning."""
        text = _text("""
            downstream:
              command: x
              schedule: "0 9 * * 1-5"
              calendar: uk-business
              depends_on:
                - {job: upstream, run_date: -1}
            upstream:
              command: x
              schedule: "0 8 * * *"
              calendar: uk-business
        """)
        assert _warnings(tmp_path, text) == ()

    def test_dependency_only_chain_is_followed(self, tmp_path: Path) -> None:
        """A dependency-only upstream gets its run dates from its own upstreams."""
        text = _text("""
            downstream:
              command: x
              schedule: "0 9 * * *"
              depends_on: [middle]
            middle:
              command: x
              depends_on: [upstream]
            upstream:
              command: x
              schedule: "0 8 * * 1-5"
        """)
        problem = _one(_warnings(tmp_path, text), "would wait forever")
        assert "'middle'" in problem.message

    def test_fires_more_than_once(self, tmp_path: Path) -> None:
        """Depending on a job that fires twice a day is a warning."""
        text = _text("""
            downstream:
              command: x
              schedule: "0 13 * * *"
              depends_on:
                - upstream
            upstream:
              command: x
              schedule: "0 8,12 * * *"
        """)
        problem = _one(_warnings(tmp_path, text), "fires more than once per run date")
        _check(problem, "downstream", _line(text, "- upstream"))

    def test_overlap_allow_with_pools(self, tmp_path: Path) -> None:
        """``overlap: allow`` with pools is a warning at the job's line."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              overlap: allow
              pools: {database: 1}
        """)
        _check(
            _one(_warnings(tmp_path, text), "overlap allow with pools"), "a", _line(text, "  a:")
        )

    def test_depends_on_disabled_job(self, tmp_path: Path) -> None:
        """Depending on a disabled job is a warning."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - b
            b:
              command: x
              schedule: "0 8 * * *"
              enabled: false
        """)
        _check(
            _one(_warnings(tmp_path, text), "depends on disabled job 'b'"), "a", _line(text, "- b")
        )


def _big_config(tmp_path: Path) -> Config:
    """Build a config of ``JOB_COUNT`` jobs, half depending on the other half."""
    lines = [HEADER]
    for i in range(0, JOB_COUNT, 2):
        lines.append(
            f"  up-{i}:\n    command: run {{run_date}}\n    calendar: uk-business\n"
            f'    schedule: "0 18 * * 1-5"\n    pools: {{database: 1}}\n'
        )
        lines.append(
            f"  down-{i}:\n    command: run {{run_date}}\n    calendar: uk-business\n"
            f'    schedule: ["0 19 * * 1-5", {{business_day: -1, at: "20:00"}}]\n'
            f"    depends_on: [up-{i}, {{job: up-{i}, run_date: -1}}]\n"
        )
    config, problems = build_config(_write(tmp_path, "".join(lines)), env={})
    assert problems == []
    return config


class TestValidateConfig:
    """``validate_config`` called directly."""

    def test_clean_config_has_no_problems(self, tmp_path: Path) -> None:
        """A sound config gives no problems."""
        assert validate_config(_big_config(tmp_path), today=TODAY) == []

    def test_fast_on_200_jobs(self, tmp_path: Path) -> None:
        """Validating 200 jobs takes under two seconds."""
        config = _big_config(tmp_path)
        start = time.perf_counter()
        validate_config(config, today=TODAY)
        assert time.perf_counter() - start < MAX_SECONDS


class TestEdgeCases:
    """Odd corners: malformed templates, sparse calendars, broken and manual upstreams."""

    def test_malformed_template(self, tmp_path: Path) -> None:
        """A template with an unclosed brace is an error naming the key."""
        text = _text("""
            a:
              command: "run {"
              schedule: "0 9 * * *"
        """)
        _check(_one(_errors(tmp_path, text), "command: invalid template"), "a", _line(text, "  a:"))

    def test_unknown_job_suggests_a_name(self, tmp_path: Path) -> None:
        """A near-miss job name gets a "did you mean" hint."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on: [extrct]
            extract:
              command: x
              schedule: "0 8 * * *"
        """)
        assert "did you mean 'extract'?" in _one(_errors(tmp_path, text), "unknown job").message

    def test_sparse_calendar_is_reported(self, tmp_path: Path) -> None:
        """A run date that cannot be worked out is an error, not a crash."""
        text = HEADER.replace(
            "calendars:\n",
            "calendars:\n  sparse:\n    weekdays: []\n    extra_days: [2026-10-20]\n",
        ) + (
            "  a:\n    command: x\n    calendar: sparse\n    run_date: previous_business_day\n"
            '    schedule: "0 9 * * *"\n'
        )
        problem = _one(_errors(tmp_path, text), "cannot work out the schedule")
        _check(problem, "a", _line(text, "  a:"))

    def test_dependency_on_broken_job_not_judged(self, tmp_path: Path) -> None:
        """A dependency on a job with loader errors adds no further problems."""
        text = _text("""
            a:
              command: x
              schedule: "0 9 * * *"
              calendar: uk-business
              depends_on: [b]
            b:
              command: x
              schedule: "0 8 * * *"
              calendar: [oops]
        """)
        problems = _errors(tmp_path, text)
        assert [p.job for p in problems] == ["b"]

    def test_manual_upstreams_are_not_judged(self, tmp_path: Path) -> None:
        """A manual-only upstream, direct or behind a dependency-only job, gives no warning."""
        text = _text("""
            downstream:
              command: x
              schedule: "0 9 * * *"
              depends_on: [by-hand, middle]
            middle:
              command: x
              depends_on: [by-hand]
            by-hand:
              command: x
              schedule: manual
        """)
        assert _warnings(tmp_path, text) == ()

    def test_disabled_job_behind_dependency_only_job(self, tmp_path: Path) -> None:
        """A dependency-only job fed only by a disabled job never gets a run."""
        text = _text("""
            downstream:
              command: x
              schedule: "0 9 * * *"
              depends_on: [middle]
            middle:
              command: x
              depends_on: [off]
            off:
              command: x
              schedule: "0 8 * * *"
              enabled: false
        """)
        warnings = _warnings(tmp_path, text)
        assert _one(warnings, "would wait forever").job == "downstream"
        assert _one(warnings, "depends on disabled job 'off'").job == "middle"
