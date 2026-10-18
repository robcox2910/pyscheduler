"""A scheduler that the crash test kills: start 3 real jobs, say READY, then wait.

Run as ``python _crash_child.py CONFIG``. It starts a real engine
(``SystemClock`` and ``MinderLauncher``) on the config's state folder,
triggers every job, and prints ``READY`` once all of them are running
durably and their minders have written their ``.pid`` files. Then it
sleeps until it is killed.
"""

import sys
import textwrap
from pathlib import Path

import pynet.loop

from pyscheduler.config import load_config
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import MinderLauncher
from pyscheduler.engine.model import RunState
from pyscheduler.store import Store

JOBS = ("one", "two", "three")
"""The jobs every crash test runs."""

READY = "READY"
"""The first word of the line the child prints (then the run ids) once every job runs."""

POLL = 0.01
"""How long (real seconds) the child naps between looks at its runs."""

FOREVER = 3600.0
"""How long the child sleeps after saying READY (it is killed long before)."""


def write_config(folder: Path) -> Path:
    """Write a config with three ``sleep 1`` jobs into ``folder``; return its path."""
    jobs = "".join(
        f"  {name}:\n    command: 'sleep 1; exit 0'\n    schedule: manual\n" for name in JOBS
    )
    text = textwrap.dedent(f"""\
        scheduler:
          state_dir: {folder / "state"}
          log_dir: {folder / "logs"}
        jobs:
        """)
    path = folder / "pysched.yaml"
    path.write_text(text + jobs, encoding="utf-8")
    return path


def pid_file(state_dir: Path, run_id: str) -> Path:
    """Return the ``.pid`` file the minder of ``run_id``'s first attempt writes."""
    return state_dir / "status" / f"{run_id}.1.json.pid"


async def main(config_path: Path) -> None:
    """Start the engine, trigger every job, say READY when all run, then wait."""
    config = load_config(config_path, env={})
    state_dir = config.settings.state_dir
    store = Store.open(state_dir)
    engine = Engine(config, store, clock=SystemClock(), launcher=MinderLauncher())
    await engine.start()
    run_ids = [(await engine.trigger(name, actor="test")).run_id for name in JOBS]
    while True:
        published = [engine.book.published(run_id) for run_id in run_ids]
        running = all(run is not None and run.state is RunState.RUNNING for run in published)
        if running and all(pid_file(state_dir, run_id).exists() for run_id in run_ids):
            break
        await pynet.loop.sleep(POLL)
    print(READY, *run_ids, flush=True)  # noqa: T201 -- the parent reads this line
    await pynet.loop.sleep(FOREVER)


if __name__ == "__main__":
    pynet.loop.run(main(Path(sys.argv[1])))
