"""Prove the engine is concurrent with real processes: many together, the cap held, the loop free."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop
import pytest

from pyscheduler.config import load_config
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import LaunchSpec, MinderHandle, MinderLauncher
from pyscheduler.engine.model import Run, RunState
from pyscheduler.store import Store

MANY = 200
"""How many jobs are started together in the big tests."""

WALL_LIMIT = 6.0
"""How long (real seconds) 200 jobs of ``sleep 0.2`` may take, from first trigger to last success."""

LAG_LIMIT = 0.05
"""The most the 99th percentile of the 10 ms timer's lateness may be (ruling R25)."""

LAG_MAX = 0.25
"""The most the single worst tick may be late (a busy test machine gets this much slack)."""

PERCENTILE = 0.99
"""Which share of the ticks must be on time (within ``LAG_LIMIT``)."""

PEAK_FLOOR = 10
"""The fewest attempts that must be alive together in the 200-job tests."""

BIG_CAP = 50
"""The ``max_running`` of the 200-job cap test (below 200, so it could be exceeded)."""

PROBE_EVERY = 0.01
"""How often the loop-lag probe ticks (seconds)."""

CAPPED = 50
"""How many jobs run in the cap test."""

CAP = 8
"""The ``max_running`` of the cap test."""

POLL = 0.02
"""How long the waiting loops nap (seconds)."""

DONE_LIMIT = 20.0
"""A safety net: how long a test waits for every run before giving up."""


class CountingLauncher(MinderLauncher):
    """A real minder launcher that counts how many attempts are alive at once."""

    def __init__(self) -> None:
        """Start with nothing running."""
        super().__init__()
        self.live = 0
        self.most = 0

    async def launch(self, spec: LaunchSpec) -> MinderHandle:
        """Launch for real and count the attempt until its wait returns."""
        handle = await super().launch(spec)
        self.live += 1
        self.most = max(self.most, self.live)
        launcher = self
        original = handle.wait

        async def wait() -> object:
            """Wait for the real handle, then count the attempt as gone."""
            try:
                return await original()
            finally:
                launcher.live -= 1

        handle.wait = wait  # type: ignore[method-assign]
        return handle


class LagProbe:
    """A repeating 10 ms timer that records how late each tick was."""

    def __init__(self) -> None:
        """Start with no ticks seen."""
        self.lateness: list[float] = []
        self._due = 0.0
        self._timer: pynet.loop.TimerHandle | None = None

    def start(self) -> None:
        """Plan the first tick."""
        self._plan()

    def stop(self) -> None:
        """Cancel the next tick."""
        if self._timer is not None:
            self._timer.cancel()

    def _plan(self) -> None:
        """Plan the next tick ``PROBE_EVERY`` from now."""
        self._due = time.monotonic() + PROBE_EVERY
        self._timer = pynet.loop.get_running_loop().call_later(PROBE_EVERY, self._tick)

    def _tick(self) -> None:
        """Record this tick's lateness and plan the next."""
        self.lateness.append(time.monotonic() - self._due)
        self._plan()

    @property
    def ticks(self) -> int:
        """Return how many ticks were seen."""
        return len(self.lateness)

    @property
    def worst(self) -> float:
        """Return the latest any tick was (seconds)."""
        return max(self.lateness, default=0.0)

    def percentile(self, share: float) -> float:
        """Return the lateness that ``share`` of the ticks were within (seconds)."""
        ordered = sorted(self.lateness)
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def _config(tmp_path: Path, count: int, max_running: int) -> Path:
    """Write a config of ``count`` manual ``sleep 0.2`` jobs and return its path."""
    lines = [
        "scheduler:",
        f"  state_dir: {tmp_path / 'state'}",
        f"  log_dir: {tmp_path / 'logs'}",
        f"  max_running: {max_running}",
        "jobs:",
    ]
    for n in range(count):
        lines += [f"  j{n:03d}:", '    command: "sleep 0.2"', "    schedule: manual"]
    path = tmp_path / "pysched.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Proof:
    """One real engine, its counting launcher and what the subscriber saw."""

    def __init__(self, tmp_path: Path, count: int, max_running: int) -> None:
        """Build the config, store and engine (with a real clock and real minders)."""
        path = _config(tmp_path, count, max_running)
        self.config = load_config(path, env={}, today=datetime.now(UTC).date())
        self.store = Store.open(tmp_path / "state")
        self.launcher = CountingLauncher()
        self.engine = Engine(self.config, self.store, clock=SystemClock(), launcher=self.launcher)
        self.probe = LagProbe()
        self.states: dict[str, RunState] = {}
        self.running = 0
        self.most_running = 0
        self.wall = 0.0

    def _seen(self, run: Run) -> None:
        """Track each run's latest durable state and the most running at once."""
        before = self.states.get(run.run_id)
        self.states[run.run_id] = run.state
        self.running += (run.state is RunState.RUNNING) - (before is RunState.RUNNING)
        self.most_running = max(self.most_running, self.running)

    def run(self) -> None:
        """Start the engine, trigger every job, wait for every success, stop."""

        async def main() -> None:
            """Drive the whole proof on one loop."""
            await self.engine.start()
            self.engine.subscribe(self._seen)
            self.probe.start()
            begun = time.monotonic()
            try:
                # All at once, as 200 jobs due at the same minute would be.
                triggers = (self.engine.trigger(name, actor="test") for name in self.config.jobs)
                await pynet.loop.gather(*triggers)
                deadline = begun + DONE_LIMIT
                while time.monotonic() < deadline:
                    done = [s for s in self.states.values() if s is RunState.SUCCEEDED]
                    if len(done) == len(self.config.jobs):
                        break
                    await pynet.loop.sleep(POLL)
                self.wall = time.monotonic() - begun
            finally:
                self.probe.stop()
                await self.engine.stop()

        pynet.loop.run(main())

    def succeeded(self) -> int:
        """Return how many runs ended in success."""
        return sum(1 for state in self.states.values() if state is RunState.SUCCEEDED)


class TestConcurrency:
    """Real minders, real clock, real store."""

    @pytest.mark.timeout(30)
    def test_two_hundred_jobs(self, tmp_path: Path) -> None:
        """Start 200 jobs together: all finish fast, many overlap, the loop stays free."""
        proof = Proof(tmp_path, MANY, MANY)
        proof.run()
        p99 = proof.probe.percentile(PERCENTILE)
        print(  # noqa: T201 -- the measured numbers, shown with ``-s``
            f"wall {proof.wall:.2f}s, lag p99 {p99 * 1000:.1f}ms "
            f"max {proof.probe.worst * 1000:.1f}ms, peak alive {proof.launcher.most}, "
            f"ticks {proof.probe.ticks}"
        )
        assert proof.succeeded() == MANY
        assert proof.wall < WALL_LIMIT
        assert PEAK_FLOOR <= proof.launcher.most <= MANY
        assert proof.probe.ticks > 0
        assert p99 < LAG_LIMIT
        assert proof.probe.worst < LAG_MAX

    @pytest.mark.timeout(30)
    def test_two_hundred_jobs_under_a_cap(self, tmp_path: Path) -> None:
        """Start 200 jobs with ``max_running: 50``: many overlap, never more than 50."""
        proof = Proof(tmp_path, MANY, BIG_CAP)
        proof.run()
        print(f"peak alive {proof.launcher.most}, peak running {proof.most_running}")  # noqa: T201
        assert proof.succeeded() == MANY
        assert PEAK_FLOOR <= proof.launcher.most <= BIG_CAP
        assert proof.most_running <= BIG_CAP

    @pytest.mark.timeout(30)
    def test_cap_respected_real(self, tmp_path: Path) -> None:
        """Run 50 jobs with ``max_running: 8``: never more than 8 running at once."""
        proof = Proof(tmp_path, CAPPED, CAP)
        proof.run()
        assert proof.succeeded() == CAPPED
        assert 0 < proof.most_running <= CAP
        assert proof.launcher.most <= CAP
