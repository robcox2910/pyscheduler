"""The remote-run test harness: an engine whose agent jobs go through a ``FakeHub``."""

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pynet.loop
from engine_helpers import START, TODAY, Harness

from pyscheduler.agents.book import RunKey
from pyscheduler.config import load_config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt
from pyscheduler.fake_hub import FakeHub
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher

AGENTS = ("build-01", "cpu-01", "gpu-01")
"""The agents named in the harness's secrets file."""

SECRET = "s" * 40
"""Every agent's shared secret (unused by the fake hub, but the config needs one)."""

SECRETS_MODE = 0o600
"""The secrets file's mode (no warning about loose permissions)."""


@dataclass
class RemoteHarness(Harness):
    """A harness whose engine places agent jobs through ``hub``."""

    hub: FakeHub

    async def attempts(self, run_id: str) -> list[Attempt]:
        """Read ``run_id``'s attempt rows from the store, off the loop thread."""
        return await pynet.loop.run_in_thread(self.store.attempts_for, run_id)

    def key(self, job: str, attempt: int = 1) -> RunKey:
        """Return the hub key of ``job``'s first run of the day, attempt ``attempt``."""
        return (self.run(job).run_id, attempt)

    def busy(self) -> int:
        """Return how many slots the hub's book holds on all agents together."""
        return sum(len(state.running) for state in self.hub.book.states())


def remote_harness(tmp_path: Path, text: str, *, settings: str = "") -> RemoteHarness:
    """Build a harness from the YAML after ``scheduler:``, with agents and a ``FakeHub``."""
    secrets = tmp_path / "agents.yaml"
    secrets.write_text("".join(f"- {{name: {n}, secret: {SECRET}}}\n" for n in AGENTS))
    secrets.chmod(SECRETS_MODE)
    write_config(tmp_path, text, settings=settings)
    clock = FakeClock(START)
    return _build(tmp_path, clock, FakeHub(clock))


def write_config(tmp_path: Path, text: str, *, settings: str = "") -> None:
    """Write the harness config: scheduler settings, an ``agents:`` section, then ``text``."""
    header = (
        "scheduler:\n"
        f"  state_dir: {tmp_path / 'state'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n"
        "  kill_grace: 10s\n" + "".join(f"  {line}\n" for line in settings.splitlines())
    )
    agents = 'agents: {listen: "127.0.0.1:8471", secrets_file: agents.yaml}\n'
    body = header + agents + textwrap.dedent(text).lstrip("\n")
    (tmp_path / "pysched.yaml").write_text(body, encoding="utf-8")


def restarted(h: RemoteHarness) -> RemoteHarness:
    """Return a harness on the same state folder and agents, as after a scheduler restart.

    The config is read again, so a test may rewrite it first.
    """
    clock = FakeClock(h.clock.now())
    return _build(h.tmp_path, clock, h.hub.restarted(clock))


def _build(tmp_path: Path, clock: FakeClock, hub: FakeHub) -> RemoteHarness:
    """Load the config and open the store, then build the engine around ``hub``."""
    config = load_config(tmp_path / "pysched.yaml", env={}, today=TODAY)
    store = Store.open(tmp_path / "state", sync=False)
    launcher = FakeLauncher(clock)
    engine = Engine(config, store, clock=clock, launcher=launcher, placement=hub)
    return RemoteHarness(tmp_path, config, store, clock, launcher, engine, hub)
