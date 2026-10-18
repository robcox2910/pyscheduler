"""Sub-minute steps are refused, and a busy schedule validates fast (ruling R30)."""

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from pyscheduler.config import ConfigError, load_config
from pyscheduler.schedule import firings

type WriteYaml = Callable[[str, str], Path]

FAST = 3.0
"""The most seconds validating an every-minute upstream may take."""

CACHE_SIZE = 64

EVERY_MINUTE = """
jobs:
  a: {command: run, schedule: "* * * * *"}
  b:
    command: run-b
    depends_on: [a]
  c:
    command: run-c
    schedule: "0 2 * * *"
    depends_on: [{job: a, run_date: -1}]
  d:
    command: run-d
    schedule: {every: 1m, between: "00:00-23:59"}
"""


class TestEveryStep:
    """``every`` steps under a minute are a validation error."""

    @pytest.mark.parametrize("step", ["30s", "59s"])
    def test_sub_minute_refused(self, write_yaml: WriteYaml, step: str) -> None:
        """Refuse a step shorter than one minute."""
        path = write_yaml(
            "pysched.yaml",
            f'jobs:\n  a: {{command: run, schedule: {{every: {step}, between: "08:00-09:00"}}}}\n',
        )
        with pytest.raises(ConfigError, match="every must be at least 1m"):
            load_config(path, env={})

    def test_one_minute_and_more_allowed(self, write_yaml: WriteYaml) -> None:
        """Accept one minute, and a minute and a half."""
        path = write_yaml(
            "pysched.yaml",
            "jobs:\n"
            '  a: {command: run, schedule: {every: 1m, between: "08:00-09:00"}}\n'
            '  b: {command: run, schedule: {every: 1m30s, between: "08:00-09:00"}}\n',
        )
        assert set(load_config(path, env={}).jobs) == {"a", "b"}


class TestBusyScheduleSpeed:
    """Validating an every-minute upstream is quick and leaves the day cache small."""

    @pytest.mark.timeout(20)
    def test_every_minute_validates_fast(self, write_yaml: WriteYaml) -> None:
        """Load (and validate) in under 3 seconds without filling the shared cache."""
        path = write_yaml("pysched.yaml", EVERY_MINUTE)
        firings._cached_day.cache_clear()
        begun = time.monotonic()
        config = load_config(path, env={})
        took = time.monotonic() - begun
        assert set(config.jobs) == {"a", "b", "c", "d"}
        assert took < FAST, took
        info = firings._cached_day.cache_info()
        assert info.maxsize == CACHE_SIZE
        assert info.currsize <= CACHE_SIZE
