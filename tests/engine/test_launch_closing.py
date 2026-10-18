"""No minder is forked once the engine is stopping (real processes)."""

import contextlib
import os
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pynet.loop
import pytest

from pyscheduler.config import load_config
from pyscheduler.engine import launcher as launcher_module
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import (
    CANCELLED_MESSAGE,
    LaunchCancelledError,
    LaunchSpec,
    MinderLauncher,
    live_minder_pids,
)
from pyscheduler.minder import Note
from pyscheduler.store import Store

JOBS = 60
"""How many jobs are triggered together."""

STOP_AFTER = 0.05
"""How long after the triggers ``stop(kill_running=True)`` is called (seconds)."""

QUIET = 0.5
"""How long to watch for late forks after ``stop`` returns (seconds)."""


def make_spec(
    tmp_path: Path, argv: list[str], *, closing: threading.Event | None = None
) -> LaunchSpec:
    """Build a launch spec writing everything under ``tmp_path``."""
    return LaunchSpec(
        run_id="job.2026-10-19.1",
        attempt=1,
        argv=tuple(argv),
        cwd=str(tmp_path),
        env=dict(os.environ),
        stdout_path=tmp_path / "logs" / "a.out",
        stderr_path=None,
        status_path=tmp_path / "status" / "a.json",
        closing=closing,
    )


def _config(tmp_path: Path) -> Path:
    """Write ``JOBS`` manual ``sleep 0.3`` jobs with room for all of them."""
    lines = [
        "scheduler:",
        f"  state_dir: {tmp_path / 'state'}",
        f"  log_dir: {tmp_path / 'logs'}",
        f"  max_running: {JOBS}",
        "jobs:",
    ]
    for n in range(JOBS):
        lines += [f"  j{n:02d}:", '    command: "sleep 0.3"', "    schedule: manual"]
    path = tmp_path / "pysched.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestClosing:
    """``stop`` and fail-stop shut the launch queue."""

    @pytest.mark.timeout(20)
    def test_no_fork_after_stop_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trigger 60 jobs, stop after 50 ms: every fork happened before ``stop`` returned."""
        forks: list[float] = []
        children: list[int] = []
        lock = threading.Lock()
        real_popen = subprocess.Popen

        def popen(*args: Any, **kwargs: Any) -> Any:
            """Start the minder for real and note when it was started."""
            child = real_popen(*args, **kwargs)
            with lock:
                forks.append(time.monotonic())
                children.append(child.pid)
            return child

        monkeypatch.setattr(launcher_module.subprocess, "Popen", popen)
        config = load_config(_config(tmp_path), env={}, today=datetime.now(UTC).date())
        store = Store.open(tmp_path / "state", sync=False)
        engine = Engine(config, store, clock=SystemClock(), launcher=MinderLauncher())
        stopped = 0.0

        async def main() -> None:
            """Trigger everything, stop soon after, then watch for stragglers."""
            nonlocal stopped
            await engine.start()
            triggers = (engine.trigger(name, actor="test") for name in config.jobs)
            await pynet.loop.gather(*triggers)
            await pynet.loop.sleep(STOP_AFTER)
            await engine.stop(kill_running=True)
            stopped = time.monotonic()
            await pynet.loop.sleep(QUIET)

        try:
            pynet.loop.run(main())
        finally:
            for child in children:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(child, signal.SIGKILL)
        assert len(forks) < JOBS  # the stop came before every job was started
        late = [when for when in forks if when > stopped]
        assert late == []

    def test_closing_spec_is_not_forked(self, tmp_path: Path) -> None:
        """A spec whose ``closing`` event is set raises the cancelled start error."""
        closing = threading.Event()
        closing.set()
        spec = make_spec(tmp_path, ["true"], closing=closing)
        before = live_minder_pids()

        async def main() -> None:
            """Try the launch."""
            with pytest.raises(LaunchCancelledError, match=CANCELLED_MESSAGE):
                await MinderLauncher().launch(spec)

        pynet.loop.run(main())
        assert live_minder_pids() == before
        assert not spec.status_path.with_name(f"{spec.status_path.name}.pid").exists()


class TestStartFailures:
    """What ``Popen`` refuses, and when a new minder is first tracked."""

    def test_nul_in_argv_raises_value_error(self, tmp_path: Path) -> None:
        """``Popen`` refuses an embedded NUL with ``ValueError`` (the engine's start error)."""
        spec = make_spec(tmp_path, ["echo", "a\x00b"])

        async def main() -> None:
            """Try the launch."""
            with pytest.raises(ValueError, match="null"):
                await MinderLauncher().launch(spec)

        pynet.loop.run(main())

    def test_pid_tracked_on_the_launch_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new minder's pid is live before the launch thread even writes its pid file."""
        seen: list[bool] = []
        real_write = launcher_module.write_status

        def write_status(path: Path, note: Note) -> None:
            """Check the pid is already tracked, then write the file."""
            seen.append(note.get("pid") in live_minder_pids())
            real_write(path, note)

        monkeypatch.setattr(launcher_module, "write_status", write_status)

        async def main() -> bool:
            """Launch ``true`` and wait for it."""
            handle = await MinderLauncher().launch(make_spec(tmp_path, ["true"]))
            return (await handle.wait()).succeeded

        assert pynet.loop.run(main())
        assert seen == [True]
