"""Shared helpers for the file-watcher engine tests."""

from pathlib import Path

import pynet.loop
from engine_helpers import Harness

from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import LaunchSpec
from pyscheduler.engine.model import Attempt
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher


def watcher_config(tmp_path: Path, *, extra: str = "", give_up: str = "1d") -> str:
    """Return a config with watcher ``in`` and ``load`` (``cat {files}``) depending on it."""
    lines = "".join(f"    {line}\n" for line in extra.strip().splitlines())
    return (
        "jobs:\n"
        "  in:\n"
        "    type: file_watcher\n"
        "    schedule: manual\n"
        f"    watch: '{tmp_path}/in_{{run_date:%Y%m%d}}.csv'\n"
        "    min_age: 30s\n"
        "    poll: 10s\n"
        f"    give_up_after: {give_up}\n"
        f"{lines}"
        "  load:\n"
        "    command: 'cat {files}'\n"
        "    depends_on: [in]\n"
    )


def specs_for(h: Harness, job: str) -> list[LaunchSpec]:
    """Return the launch specs of ``job``'s attempts, oldest first."""
    return [s for s in h.launcher.launched if s.run_id.split(".", 1)[0] == job]


async def attempts(h: Harness, run_id: str) -> list[Attempt]:
    """Read the attempts of ``run_id`` from the store, off the loop thread."""
    return await pynet.loop.run_in_thread(h.store.attempts_for, run_id)


def restarted(h: Harness, clock: FakeClock) -> Harness:
    """Return a new engine on ``h``'s (stopped) store folder, with ``clock``."""
    store = Store.open(h.tmp_path / "state", sync=False)
    launcher = FakeLauncher(clock)
    engine = Engine(h.config, store, clock=clock, launcher=launcher)
    return Harness(h.tmp_path, h.config, store, clock, launcher, engine)
