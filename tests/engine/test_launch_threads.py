"""The minder is started on a launch thread, never on the loop, and a cancelled launch is not lost."""

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pynet.loop
import pytest

from pyscheduler.engine import launcher as launcher_module
from pyscheduler.engine.launcher import LaunchSpec, MinderLauncher, live_minder_pids

WAIT_SECONDS = 5.0
POLL = 0.01


def make_spec(tmp_path: Path, argv: list[str]) -> LaunchSpec:
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
    )


class TestLaunchThread:
    """Forking and the pid file's fsync happen off the loop."""

    def test_fork_happens_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Popen`` is called on a launch thread, not on the loop's thread."""
        threads: list[threading.Thread] = []
        real_popen = subprocess.Popen

        def popen(*args: Any, **kwargs: Any) -> Any:
            """Note the calling thread, then start the process for real."""
            threads.append(threading.current_thread())
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(launcher_module.subprocess, "Popen", popen)

        async def main() -> bool:
            """Launch ``true`` and wait for it."""
            handle = await MinderLauncher().launch(make_spec(tmp_path, ["true"]))
            return (await handle.wait()).succeeded

        assert pynet.loop.run(main())
        assert threads
        assert threads[0] is not threading.main_thread()

    def test_cancelled_launch_is_still_tracked_and_reaped(self, tmp_path: Path) -> None:
        """A launch cancelled mid-start leaves a minder that is tracked until it exits."""
        spec = make_spec(tmp_path, ["true"])
        before = live_minder_pids()

        async def main() -> None:
            """Cancel the launch at once, then wait until the orphan minder is forgotten."""
            task = pynet.loop.spawn(MinderLauncher().launch(spec))
            await pynet.loop.sleep(0)
            task.cancel()
            with pytest.raises(pynet.loop.Cancelled):
                await task
            async with pynet.loop.timeout(WAIT_SECONDS):
                while not spec.status_path.exists():  # the minder ran to the end
                    await pynet.loop.sleep(POLL)
                while live_minder_pids() - before:
                    await pynet.loop.sleep(POLL)

        pynet.loop.run(main())
        assert live_minder_pids() == before
        pid_note = spec.status_path.with_name(f"{spec.status_path.name}.pid")
        pid = json.loads(pid_note.read_text())["pid"]
        with pytest.raises(ChildProcessError):  # the child watcher already reaped it
            os.waitpid(pid, os.WNOHANG)
