"""Shared helpers for the validation tests: build configs and pick out problems."""

import textwrap
from datetime import date
from pathlib import Path

import pytest

from pyscheduler.config import ConfigError, Problem, load_config

TODAY = date(2026, 10, 18)
"""A fixed day for the validation horizons, so results never drift."""

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


def jobs_text(jobs: str) -> str:
    """Return the header followed by ``jobs`` (dedented, indented under ``jobs:``)."""
    body = textwrap.dedent(jobs).strip("\n")
    return HEADER + "".join(f"  {line}\n" for line in body.splitlines())


def line_of(text: str, needle: str) -> int:
    """Return the 1-based line of the first line of ``text`` containing ``needle``."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    msg = f"{needle!r} not in text"
    raise AssertionError(msg)


def write_config(tmp_path: Path, text: str) -> Path:
    """Write ``text`` to ``scheduler.yaml`` under ``tmp_path``."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def errors_of(tmp_path: Path, text: str) -> tuple[Problem, ...]:
    """Load ``text`` and return the problems of the ``ConfigError`` it raises."""
    with pytest.raises(ConfigError) as info:
        load_config(write_config(tmp_path, text), env={}, today=TODAY)
    return info.value.problems


def warnings_of(tmp_path: Path, text: str) -> tuple[Problem, ...]:
    """Load ``text`` (which must have no errors) and return its warnings."""
    return load_config(write_config(tmp_path, text), env={}, today=TODAY).warnings


def only(problems: tuple[Problem, ...], phrase: str) -> Problem:
    """Return the only problem whose message contains ``phrase``."""
    found = [p for p in problems if phrase in p.message]
    assert len(found) == 1, problems
    return found[0]


def check(problem: Problem, job: str | None, line: int) -> None:
    """Assert the problem's job and line."""
    assert problem.job == job
    assert problem.line == line
    assert problem.file.endswith("scheduler.yaml")
