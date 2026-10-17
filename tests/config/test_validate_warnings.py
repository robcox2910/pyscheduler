"""Validation tests: warnings, direct calls and speed."""

import time
from pathlib import Path

from validate_helpers import (
    HEADER,
    TODAY,
    check,
    jobs_text,
    line_of,
    only,
    warnings_of,
    write_config,
)

from pyscheduler.config import Config, build_config
from pyscheduler.config.validate import validate_config

JOB_COUNT = 200
MAX_SECONDS = 2.0
CHAIN_LENGTH = 1000


class TestWarnings:
    """Warnings that do not stop the load."""

    def test_would_wait_forever(self, tmp_path: Path) -> None:
        """A daily job depending on a Monday-to-Friday job waits forever at weekends."""
        text = jobs_text("""
            downstream:
              command: x
              schedule: "0 9 * * *"
              depends_on:
                - upstream
            upstream:
              command: x
              schedule: "0 8 * * 1-5"
        """)
        problem = only(warnings_of(tmp_path, text), "would wait forever")
        assert problem.severity == "warning"
        check(problem, "downstream", line_of(text, "- upstream"))

    def test_matching_schedules_do_not_wait(self, tmp_path: Path) -> None:
        """Upstream and downstream on the same weekdays give no warning."""
        text = jobs_text("""
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
        assert warnings_of(tmp_path, text) == ()

    def test_dependency_only_chain_is_followed(self, tmp_path: Path) -> None:
        """A dependency-only upstream gets its run dates from its own upstreams."""
        text = jobs_text("""
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
        problem = only(warnings_of(tmp_path, text), "would wait forever")
        assert "needs a run of 'middle' for 2026-10-18" in problem.message
        assert "'upstream' needs a run for 2026-10-18 but has no firing for it" in problem.message

    def test_direct_upstream_message(self, tmp_path: Path) -> None:
        """The message names the first run date and the upstream that lacks it."""
        text = jobs_text("""
            downstream:
              command: x
              schedule: "0 9 * * *"
              depends_on: [upstream]
            upstream:
              command: x
              schedule: "0 8 * * 1-5"
        """)
        problem = only(warnings_of(tmp_path, text), "would wait forever")
        assert problem.message.startswith(
            "would wait forever: its run for 2026-10-18 needs a run of 'upstream' for 2026-10-18, "
            "which has no firing for it"
        )

    def test_fires_more_than_once(self, tmp_path: Path) -> None:
        """Depending on a job that fires twice a day is a warning."""
        text = jobs_text("""
            downstream:
              command: x
              schedule: "0 13 * * *"
              depends_on:
                - upstream
            upstream:
              command: x
              schedule: "0 8,12 * * *"
        """)
        problem = only(warnings_of(tmp_path, text), "fires more than once per run date")
        check(problem, "downstream", line_of(text, "- upstream"))

    def test_overlap_allow_with_pools(self, tmp_path: Path) -> None:
        """``overlap: allow`` with pools is a warning at the job's line."""
        text = jobs_text("""
            a:
              command: x
              schedule: "0 9 * * *"
              overlap: allow
              pools: {database: 1}
        """)
        check(
            only(warnings_of(tmp_path, text), "overlap allow with pools"),
            "a",
            line_of(text, "  a:"),
        )

    def test_depends_on_disabled_job(self, tmp_path: Path) -> None:
        """Depending on a disabled job is a warning."""
        text = jobs_text("""
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
        check(
            only(warnings_of(tmp_path, text), "depends on disabled job 'b'"),
            "a",
            line_of(text, "- b"),
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
    config, problems = build_config(write_config(tmp_path, "".join(lines)), env={})
    assert problems == []
    return config


def _chain_config(tmp_path: Path, length: int, root_schedule: str) -> Config:
    """Build a scheduled root, ``length`` dependency-only followers and a daily end job."""
    parts = [HEADER, f'  j0:\n    command: x\n    schedule: "{root_schedule}"\n']
    parts.extend(
        f"  j{i}:\n    command: x\n    depends_on: [j{i - 1}]\n" for i in range(1, length + 1)
    )
    parts.append(
        f'  end:\n    command: x\n    schedule: "0 9 * * *"\n    depends_on: [j{length}]\n'
    )
    config, problems = build_config(write_config(tmp_path, "".join(parts)), env={})
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

    def test_long_dependency_only_chain(self, tmp_path: Path) -> None:
        """A 1000-job follower chain is walked without recursion and blamed on its root."""
        config = _chain_config(tmp_path, CHAIN_LENGTH, "0 8 * * 1-5")
        start = time.perf_counter()
        problems = validate_config(config, today=TODAY)
        assert time.perf_counter() - start < MAX_SECONDS
        problem = only(tuple(problems), "would wait forever")
        assert problem.job == "end"
        assert "'j0' needs a run for 2026-10-18" in problem.message

    def test_long_chain_that_keeps_up(self, tmp_path: Path) -> None:
        """A daily root feeding a 1000-job chain gives a daily end job no warning."""
        config = _chain_config(tmp_path, CHAIN_LENGTH, "0 8 * * *")
        assert validate_config(config, today=TODAY) == []
