"""Test the two-agent demo: both jobs succeed, each on the expected agent."""

import time
from pathlib import Path

import pytest

from pyscheduler.demo_agents import PINNED_JOB, TAGGED_JOB, run_agents_demo

BOUND_SECONDS = 8.0
"""The demo must finish within this long."""

LIMIT_SECONDS = 30.0
"""The demo's own generous limit, so a loaded machine fails loudly rather than flakily."""


def test_demo_runs_each_job_on_its_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pinned job runs on ``build-01``, the ``gpu`` job on ``gpu-01``, within the bound."""
    started = time.monotonic()
    run_agents_demo(tmp_path / "demo", seconds=LIMIT_SECONDS)
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out.splitlines()
    assert out == [f"{PINNED_JOB} ran on build-01", f"{TAGGED_JOB} ran on gpu-01"]
    assert elapsed < BOUND_SECONDS


def test_ready_is_called_with_the_url(tmp_path: Path) -> None:
    """``ready`` hears the scheduler's address once it is listening."""
    urls: list[str] = []
    run_agents_demo(tmp_path / "demo", seconds=LIMIT_SECONDS, ready=urls.append)
    [url] = urls
    assert url.startswith("http://127.0.0.1:")
