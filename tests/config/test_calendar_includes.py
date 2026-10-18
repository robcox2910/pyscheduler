"""Calendar ``include`` is transitive, and a cycle does not loop."""

from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from pyscheduler.config import ConfigError, load_config

type WriteYaml = Callable[[str, str], Path]

A_DAY = date(2026, 12, 24)
B_DAY = date(2026, 12, 25)
C_DAY = date(2026, 12, 26)


class TestTransitiveIncludes:
    """``a`` includes ``b``, which includes ``c``: ``a`` has all three's holidays."""

    def test_chain(self, write_yaml: WriteYaml) -> None:
        """Holidays travel the whole chain."""
        path = write_yaml(
            "pysched.yaml",
            f"""
            calendars:
              a: {{holidays: [{A_DAY}], include: [b]}}
              b: {{holidays: [{B_DAY}], include: [c]}}
              c: {{holidays: [{C_DAY}]}}
            jobs:
              j: {{command: run, schedule: manual}}
            """,
        )
        config = load_config(path, env={})
        assert config.calendars["a"].holidays == {A_DAY, B_DAY, C_DAY}
        assert config.calendars["b"].holidays == {B_DAY, C_DAY}

    def test_cycle_is_reported_not_looped(self, write_yaml: WriteYaml) -> None:
        """``a -> b -> a`` ends, and validation still reports the cycle."""
        path = write_yaml(
            "pysched.yaml",
            f"""
            calendars:
              a: {{holidays: [{A_DAY}], include: [b]}}
              b: {{holidays: [{B_DAY}], include: [a]}}
            jobs:
              j: {{command: run, schedule: manual}}
            """,
        )
        with pytest.raises(ConfigError, match="cycle"):
            load_config(path, env={})
