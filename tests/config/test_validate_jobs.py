"""Validation tests: schedules, resources, job settings and odd corners."""

from pathlib import Path

import pytest
from validate_helpers import (
    HEADER,
    TODAY,
    check,
    errors_of,
    jobs_text,
    line_of,
    only,
    warnings_of,
    write_config,
)

from pyscheduler.config import load_config

FOUR_PROBLEMS = 4


class TestScheduleErrors:
    """Errors about schedules and time zones."""

    def test_business_day_needs_calendar(self, tmp_path: Path) -> None:
        """A business_day schedule on a job with no calendar is an error."""
        text = jobs_text("""
            a:
              command: x
              schedule: {business_day: 1, at: "07:00"}
        """)
        problem = only(errors_of(tmp_path, text), "business_day needs a calendar")
        check(problem, "a", line_of(text, "  a:"))

    def test_business_day_out_of_range(self, tmp_path: Path) -> None:
        """A business_day beyond any month's length is an error, found quickly."""
        text = jobs_text("""
            a:
              command: x
              calendar: uk-business
              schedule: {business_day: 1000000000, at: "07:00"}
        """)
        problem = only(errors_of(tmp_path, text), "business_day must be between -31 and 31")
        check(problem, "a", line_of(text, "  a:"))

    def test_never_fires(self, tmp_path: Path) -> None:
        """A schedule for 30 February never fires."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 0 30 2 *"
        """)
        problem = only(errors_of(tmp_path, text), "never fires")
        check(problem, "a", line_of(text, "  a:"))

    def test_empty_days_never_fires(self, tmp_path: Path) -> None:
        """An empty ``days`` filter never fires."""
        text = jobs_text("""
            a:
              command: x
              schedule: {at: "07:00", days: []}
        """)
        only(errors_of(tmp_path, text), "never fires")

    def test_calendar_without_business_days(self, tmp_path: Path) -> None:
        """A calendar with no weekdays and no extra days is an error; its jobs are not scanned."""
        text = HEADER.replace("calendars:\n", "calendars:\n  empty:\n    weekdays: []\n") + (
            '  a:\n    command: x\n    calendar: empty\n    schedule: "0 9 * * *"\n'
        )
        problems = errors_of(tmp_path, text)
        check(
            only(problems, "calendar 'empty' has no business days"), None, line_of(text, "empty:")
        )
        assert not [p for p in problems if "never fires" in p.message]

    def test_unknown_time_zone(self, tmp_path: Path) -> None:
        """An unknown job time zone is reported (by the loader) at its line."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              timezone: Mars/Base
        """)
        check(only(errors_of(tmp_path, text), "unknown time zone"), "a", line_of(text, "Mars/Base"))


class TestResourceErrors:
    """Errors about calendars, pools and placeholders."""

    def test_unknown_calendar(self, tmp_path: Path) -> None:
        """A job naming an undefined calendar is an error."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              calendar: nope
        """)
        check(only(errors_of(tmp_path, text), "unknown calendar"), "a", line_of(text, "  a:"))

    def test_unknown_pool(self, tmp_path: Path) -> None:
        """A job naming an undefined pool is an error."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              pools: {nope: 1}
        """)
        check(only(errors_of(tmp_path, text), "unknown pool"), "a", line_of(text, "  a:"))

    def test_too_many_slots(self, tmp_path: Path) -> None:
        """A job needing more slots than the pool has is an error."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              pools: {database: 3}
        """)
        problem = only(errors_of(tmp_path, text), "needs 3 slots but pool 'database' has 2")
        check(problem, "a", line_of(text, "  a:"))

    def test_calendar_include_cycle(self, tmp_path: Path) -> None:
        """Calendars including each other are an error, reported once."""
        text = (
            "calendars:\n  x:\n    include: [y]\n  y:\n    include: [x]\n"
            'jobs:\n  a:\n    command: x\n    schedule: "0 9 * * *"\n'
        )
        problem = only(errors_of(tmp_path, text), "calendar include cycle")
        assert "x -> y -> x" in problem.message
        check(problem, None, line_of(text, "  x:"))

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
        text = jobs_text('a:\n  schedule: "0 9 * * *"\n')
        if key != "command":
            text += "    command: x\n"
        text += f"    {key}: {written}\n"
        problem = only(errors_of(tmp_path, text), "unknown placeholder {rundate}")
        assert key in problem.message
        check(problem, "a", line_of(text, "  a:"))

    def test_command_list_placeholders(self, tmp_path: Path) -> None:
        """Each argument of a command list is checked; known placeholders pass."""
        text = jobs_text("""
            a:
              command: [run, "{run_date:%Y%m%d}", "{oops}"]
              schedule: "0 9 * * *"
        """)
        only(errors_of(tmp_path, text), "unknown placeholder {oops}")


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
            ("agent: box1", "no agents: section"),
        ],
    )
    def test_job_setting(self, tmp_path: Path, keys: str, phrase: str) -> None:
        """Each bad job setting is reported at the job's line."""
        extra = "".join(f"    {line}\n" for line in keys.splitlines())
        text = jobs_text('a:\n  command: x\n  schedule: "0 9 * * *"\n') + extra
        check(only(errors_of(tmp_path, text), phrase), "a", line_of(text, "  a:"))

    def test_no_way_to_run(self, tmp_path: Path) -> None:
        """A job with no schedule, no dependencies and not manual can never run."""
        text = jobs_text("""
            a:
              command: x
        """)
        problem = only(errors_of(tmp_path, text), "no schedule, no dependencies and not manual")
        check(problem, "a", line_of(text, "  a:"))

    def test_manual_job_is_fine(self, tmp_path: Path) -> None:
        """A manual-only job needs nothing else."""
        text = jobs_text("""
            a:
              command: x
              schedule: manual
        """)
        assert load_config(write_config(tmp_path, text), env={}, today=TODAY).warnings == ()

    def test_loader_errors_are_not_repeated(self, tmp_path: Path) -> None:
        """A job whose schedule failed to load does not also get "no schedule"."""
        text = jobs_text("""
            a:
              command: x
              schedule: "bad cron"
            b:
              command: x
              schedule: "0 9 * * *"
              depends_on: [a]
        """)
        problems = errors_of(tmp_path, text)
        assert len(problems) == 1
        assert "bad cron" in problems[0].message

    def test_all_reported_together(self, tmp_path: Path) -> None:
        """Four different errors come back in one ConfigError."""
        text = jobs_text("""
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
        problems = errors_of(tmp_path, text)
        assert len(problems) == FOUR_PROBLEMS
        assert [p.job for p in problems] == ["a", "b", "c", "d"]


class TestEdgeCases:
    """Odd corners: malformed templates, sparse calendars, broken and manual upstreams."""

    def test_malformed_template(self, tmp_path: Path) -> None:
        """A template with an unclosed brace is an error naming the key."""
        text = jobs_text("""
            a:
              command: "run {"
              schedule: "0 9 * * *"
        """)
        check(
            only(errors_of(tmp_path, text), "command: invalid template"), "a", line_of(text, "  a:")
        )

    def test_unknown_job_suggests_a_name(self, tmp_path: Path) -> None:
        """A near-miss job name gets a "did you mean" hint."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              depends_on: [extrct]
            extract:
              command: x
              schedule: "0 8 * * *"
        """)
        assert "did you mean 'extract'?" in only(errors_of(tmp_path, text), "unknown job").message

    def test_sparse_calendar_is_reported(self, tmp_path: Path) -> None:
        """A run date that cannot be worked out is an error, not a crash."""
        text = HEADER.replace(
            "calendars:\n",
            "calendars:\n  sparse:\n    weekdays: []\n    extra_days: [2026-10-20]\n",
        ) + (
            "  a:\n    command: x\n    calendar: sparse\n    run_date: previous_business_day\n"
            '    schedule: "0 9 * * *"\n'
        )
        problem = only(errors_of(tmp_path, text), "cannot work out the schedule")
        check(problem, "a", line_of(text, "  a:"))

    def test_dependency_on_broken_job_not_judged(self, tmp_path: Path) -> None:
        """A dependency on a job with loader errors adds no further problems."""
        text = jobs_text("""
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
        problems = errors_of(tmp_path, text)
        assert [p.job for p in problems] == ["b"]

    def test_manual_upstreams_are_not_judged(self, tmp_path: Path) -> None:
        """A manual-only upstream, direct or behind a dependency-only job, gives no warning."""
        text = jobs_text("""
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
        assert warnings_of(tmp_path, text) == ()

    def test_disabled_job_behind_dependency_only_job(self, tmp_path: Path) -> None:
        """A dependency-only job fed only by a disabled job never gets a run."""
        text = jobs_text("""
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
        warnings = warnings_of(tmp_path, text)
        assert only(warnings, "would wait forever").job == "downstream"
        assert only(warnings, "depends on disabled job 'off'").job == "middle"
