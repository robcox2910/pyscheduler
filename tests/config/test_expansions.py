"""Test that the loader remembers every ``${NAME}`` it expands."""

from collections.abc import Callable
from pathlib import Path

from pyscheduler.config import load_config
from pyscheduler.config.fields import RecordingEnv

type WriteYaml = Callable[[str, str], Path]


class TestExpansions:
    """Record expanded values so they can be hidden again."""

    def test_records_each_expansion(self, write_yaml: WriteYaml) -> None:
        """Map each expanded value back to its name; unused variables are not recorded."""
        path = write_yaml(
            "pysched.yaml",
            """
            jobs:
              a:
                command: run --token ${TOKEN}
                schedule: manual
                env: {HOME_DIR: "${HOME_DIR}", PRICE: "$$5"}
            """,
        )
        env = {"TOKEN": "t0ken-value", "HOME_DIR": "/home/me", "UNUSED": "nope"}
        config = load_config(path, env=env)
        assert dict(config.expansions) == {"t0ken-value": "TOKEN", "/home/me": "HOME_DIR"}

    def test_no_expansions(self, write_yaml: WriteYaml) -> None:
        """Leave the map empty when nothing was expanded."""
        path = write_yaml("pysched.yaml", "jobs:\n  a: {command: run, schedule: manual}\n")
        assert dict(load_config(path, env={"X": "y"}).expansions) == {}


class TestRecordingEnv:
    """Behave like the mapping it wraps."""

    def test_mapping_behaviour(self) -> None:
        """Count, iterate and test membership without recording a use."""
        env = RecordingEnv({"A": "1", "B": "2"})
        assert len(env) == len({"A", "B"})
        assert sorted(env) == ["A", "B"]
        assert "A" in env
        assert "Z" not in env
        assert env.used == {}
        assert env["B"] == "2"
        assert env.used == {"B": "2"}
