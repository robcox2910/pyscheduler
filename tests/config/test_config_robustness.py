"""Regression tests: nested defaults merge, and odd input never crashes the loader."""

import textwrap
from collections.abc import Callable
from datetime import time
from pathlib import Path

from pyscheduler.config import AlertKind, JobAlerts, Problem, build_config

type WriteYaml = Callable[[str, str], Path]

LINE_TWO = 2
LINE_THREE = 3
DEEP_NESTING = 5000
HUGE_DIGITS = 5000
POOL_SLOTS = 2


def _job(name: str, body: str) -> str:
    """Return a ``jobs:`` document with one job whose keys are ``body`` lines."""
    lines = "".join(f"    {line}\n" for line in textwrap.dedent(body).strip().splitlines())
    return f"jobs:\n  {name}:\n{lines}"


def _errors(path: Path) -> list[Problem]:
    """Build a config and return its problems."""
    _, problems = build_config(path, env={})
    return problems


def _one_message(path: Path) -> str:
    """Return the message of the single problem found in ``path``."""
    problems = _errors(path)
    assert len(problems) == 1, problems
    return problems[0].message


class TestNestedDefaults:
    """Every nested mapping in ``defaults:`` merges key by key."""

    def test_env_pools_alerts_merge(self, write_yaml: WriteYaml) -> None:
        """Job keys win; keys only in defaults still show through."""
        text = textwrap.dedent("""
            defaults:
              env: {A: "1", B: "2"}
              pools: {db: 1, cpu: 1}
              alerts: {on: [retrying], notify: [a]}
            """).lstrip() + _job(
            "j",
            """
            command: x
            schedule: manual
            env: {B: "3"}
            pools: {db: 2}
            alerts: {off: [failed], notify: [b]}
            """,
        )
        config, problems = build_config(write_yaml("s.yaml", text), env={})
        assert problems == []
        job = config.jobs["j"]
        assert job.env == {"A": "1", "B": "3"}
        assert job.pools == {"db": POOL_SLOTS, "cpu": 1}
        assert job.alerts == JobAlerts(
            on=frozenset({AlertKind.RETRYING}), off=frozenset({AlertKind.FAILED}), notify=("b",)
        )


class TestNoCrashes:
    """Input that used to raise is reported as a problem instead."""

    def test_huge_integer(self, write_yaml: WriteYaml) -> None:
        """A many-thousand-digit number is too large, not a crash."""
        text = f"pools:\n  db: {'9' * HUGE_DIGITS}\n"
        assert "too large" in _one_message(write_yaml("s.yaml", text))

    def test_unicode_digit_port(self, write_yaml: WriteYaml) -> None:
        """A port written with a Unicode digit is rejected cleanly."""
        text = "scheduler:\n  listen: host:\u00b2\n"
        assert "host:port" in _one_message(write_yaml("s.yaml", text))

    def test_main_file_not_utf8(self, tmp_path: Path) -> None:
        """A main file that is not UTF-8 is a problem."""
        path = tmp_path / "s.yaml"
        path.write_bytes(b"jobs: \xff\xfe\n")
        assert "not valid UTF-8" in _one_message(path)

    def test_included_file_not_utf8(self, write_yaml: WriteYaml, tmp_path: Path) -> None:
        """An included file that is not UTF-8 is a problem at the include line."""
        main = write_yaml("main.yaml", "include: [bad.yaml]\n")
        (tmp_path / "bad.yaml").write_bytes(b"\xff")
        problems = _errors(main)
        assert len(problems) == 1
        assert (problems[0].file, problems[0].line) == (str(main), 1)
        assert "not valid UTF-8" in problems[0].message

    def test_deep_nesting(self, write_yaml: WriteYaml) -> None:
        """Absurdly deep nesting is reported at line 1."""
        text = "a: " + "[" * DEEP_NESTING + "]" * DEEP_NESTING + "\n"
        problems = _errors(write_yaml("s.yaml", text))
        assert len(problems) == 1
        assert problems[0].line == 1
        assert "nesting too deep" in problems[0].message


class TestMinorFixes:
    """Small correctness fixes from review."""

    def test_calendar_include_lines(self, write_yaml: WriteYaml) -> None:
        """A bad include item is reported once and later lines stay right."""
        text = "calendars:\n  c:\n    include:\n      - {x: 1}\n      - missing\n"
        problems = _errors(write_yaml("s.yaml", text))
        assert [p.line for p in problems] == [4, 5]
        assert "unknown calendar 'missing'" in problems[1].message

    def test_calendar_include_not_list(self, write_yaml: WriteYaml) -> None:
        """A non-list include is reported once."""
        text = "calendars:\n  c:\n    include: other\n"
        assert "must be a list" in _one_message(write_yaml("s.yaml", text))

    def test_bad_first_job_still_seen(self, write_yaml: WriteYaml) -> None:
        """A duplicate is flagged even when the first definition was bad."""
        main = write_yaml("main.yaml", "include: [b.yaml]\njobs:\n  j: oops\n")
        write_yaml("b.yaml", "jobs:\n  j: {command: x}\n")
        messages = [p.message for p in _errors(main)]
        assert any("duplicate job 'j'" in m for m in messages)

    def test_bad_first_calendar_still_seen(self, write_yaml: WriteYaml) -> None:
        """A duplicate calendar is flagged even when the first was bad."""
        main = write_yaml("main.yaml", "include: [b.yaml]\ncalendars:\n  c: oops\n")
        write_yaml("b.yaml", "calendars:\n  c: {}\n")
        messages = [p.message for p in _errors(main)]
        assert any("duplicate calendar 'c'" in m for m in messages)

    def test_no_double_report_in_include(self, write_yaml: WriteYaml) -> None:
        """A non-mapping ``scheduler`` in an include is reported only once."""
        main = write_yaml("main.yaml", "include: [b.yaml]\n")
        write_yaml("b.yaml", "scheduler: 5\ndefaults: 6\n")
        problems = _errors(main)
        assert len(problems) == LINE_TWO
        assert all("only allowed in the main file" in p.message for p in problems)

    def test_paths_normalised(self, write_yaml: WriteYaml) -> None:
        """Problem files are normalised (no ``..`` segments)."""
        write_yaml("sub/x.yaml", "")
        main = write_yaml("main.yaml", "include: [sub/../bad.yaml]\n")
        write_yaml("bad.yaml", "jobs:\n  j: {command: x, enabled: no}\n")
        problems = _errors(main)
        assert problems[0].file == str(main.parent / "bad.yaml")

    def test_must_start_by_needs_hh_mm(self, write_yaml: WriteYaml) -> None:
        """``9:30`` is not a valid ``must_start_by`` and the message says HH:MM."""
        path = write_yaml("s.yaml", _job("j", 'command: x\nmust_start_by: "9:30"'))
        assert "HH:MM" in _one_message(path)
        ok = write_yaml("t.yaml", _job("j", 'command: x\nmust_start_by: "09:30"'))
        config, _ = build_config(ok, env={})
        assert config.jobs["j"].must_start_by == time(9, 30)

    def test_normalised_main_line(self, write_yaml: WriteYaml) -> None:
        """A problem in the main file reports its normalised path."""
        path = write_yaml("s.yaml", "jobs:\n  j: {command: x}\n  k: 5\n")
        problems = _errors(path)
        assert (problems[0].file, problems[0].line) == (str(path), LINE_THREE)
