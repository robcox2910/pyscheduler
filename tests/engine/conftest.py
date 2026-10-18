"""Shared engine fixture: build a harness from a YAML string."""

import textwrap
from pathlib import Path

import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness

from pyscheduler.config import load_config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher


@pytest.fixture
def harness(tmp_path: Path) -> MakeHarness:
    """Return a factory that builds a ``Harness`` from the YAML after ``scheduler:``."""

    def make(text: str, *, settings: str = "") -> Harness:
        """Write the config, load it, and build the store, fakes and engine."""
        header = (
            "scheduler:\n"
            f"  state_dir: {tmp_path / 'state'}\n"
            f"  log_dir: {tmp_path / 'logs'}\n"
            "  kill_grace: 10s\n" + "".join(f"  {line}\n" for line in settings.splitlines())
        )
        path = tmp_path / "pysched.yaml"
        path.write_text(header + textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        config = load_config(path, env={}, today=TODAY)
        store = Store.open(tmp_path / "state", sync=False)
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)
        engine = Engine(config, store, clock=clock, launcher=launcher)
        return Harness(tmp_path, config, store, clock, launcher, engine)

    return make
