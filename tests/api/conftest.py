"""Shared API fixture: an engine on a fake clock and a ``TestClient`` for its app."""

import textwrap
from pathlib import Path

import pytest
from api_helpers import JOBS, START, TODAY, ApiHarness, MakeApi, tokens_yaml
from pyweb.testing import TestClient

from pyscheduler.api import TokenTable, build_app, config_actions
from pyscheduler.config import load_config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher


@pytest.fixture
def api(tmp_path: Path) -> MakeApi:
    """Return a factory that builds an ``ApiHarness`` from the YAML after ``scheduler:``."""

    def make(
        text: str = JOBS, *, tokens: bool = True, env: dict[str, str] | None = None
    ) -> ApiHarness:
        """Write the config and tokens, then build the store, fakes, engine and app."""
        header = (
            "scheduler:\n"
            f"  state_dir: {tmp_path / 'state'}\n"
            f"  log_dir: {tmp_path / 'logs'}\n"
            "  kill_grace: 10s\n"
        )
        path = tmp_path / "pysched.yaml"
        path.write_text(header + textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        config = load_config(path, env=env or {}, today=TODAY)
        store = Store.open(tmp_path / "state", sync=False)
        clock = FakeClock(START)
        launcher = FakeLauncher(clock)
        engine = Engine(config, store, clock=clock, launcher=launcher)
        table = None
        if tokens:
            tokens_path = tmp_path / "tokens.yaml"
            tokens_path.write_text(tokens_yaml(), encoding="utf-8")
            table = TokenTable.load(tokens_path)
        reload, validate = config_actions(engine, path, env=env or {})
        app = build_app(engine, tokens=table, reload=reload, validate=validate)
        return ApiHarness(tmp_path, path, config, store, clock, launcher, engine, TestClient(app))

    return make
