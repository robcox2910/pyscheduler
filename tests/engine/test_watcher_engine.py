"""Test file watchers in the engine: matched files, slots, give-up, retries, kills and restarts."""

import shlex
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness
from monitor_helpers import monitored, raised, run_monitor, step

from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.config.model import AlertKind
from pyscheduler.engine import watcher
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import LaunchSpec
from pyscheduler.engine.model import Attempt, Outcome, RunState
from pyscheduler.store import Batch, Store
from pyscheduler.testing import FakeLauncher

ACTOR = "alice"
ONE_MINUTE = timedelta(minutes=1)
MIN_AGE = timedelta(seconds=30)
POLL = timedelta(seconds=10)
STOPPED_AT = START + timedelta(seconds=40)
WATCHERS = 3
GAVE_UP = "gave up: no stable file after 1m"
FILE_NAME = "in_20261019.csv"
OLD_DAYS = 10
LATE_LOOK = 0.1
SLOW_COMMIT = 0.3


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


class TestFiles:
    """A watcher's matched files reach the jobs that depend on it."""

    def test_watcher_run_succeeds_and_feeds_downstream(
        self, harness: MakeHarness, tmp_path: Path
    ) -> None:
        """Find the file once still for min_age; ``load`` gets it in env and argv."""
        h = harness(watcher_config(tmp_path))
        path = str(tmp_path / FILE_NAME)

        async def body() -> None:
            """Trigger, create the file, and advance until the watcher has seen it still."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await h.advance(POLL)
            assert h.run("in").state is RunState.RUNNING
            Path(path).write_text("a,b\n", encoding="utf-8")
            await h.advance(POLL + MIN_AGE)
            assert h.run("in").state is RunState.SUCCEEDED
            assert h.engine.files_for(run.run_id) == (path,)
            rows = await attempts(h, run.run_id)
            assert rows[0].outcome is Outcome.SUCCEEDED
            assert rows[0].pid is None
            assert rows[0].stdout_path == str(
                tmp_path / "logs" / "in" / "2026-10-19" / f"{run.run_id}.1.out"
            )
            assert (rows[0].stderr_path, rows[0].status_path) == ("", "")
            assert f"found {path}" in Path(rows[0].stdout_path).read_text(encoding="utf-8")
            (spec,) = specs_for(h, "load")
            assert spec.env["PYSCHED_FILES"] == path
            assert shlex.join([path]) in spec.argv[-1]
            assert spec.argv[-1] == f"cat {shlex.join([path])}"
            assert h.engine.files_for("nobody.2026-10-19.1") == ()

        h.run_engine(body)

    def test_files_survive_restart(self, harness: MakeHarness, tmp_path: Path) -> None:
        """A rerun of ``load`` after a restart still gets the watcher's files."""
        h = harness(watcher_config(tmp_path))
        path = str(tmp_path / FILE_NAME)
        Path(path).write_text("x", encoding="utf-8")
        load_id = "load.2026-10-19.1"

        async def first() -> None:
            """Run the watcher and ``load`` to the end."""
            await h.engine.trigger("in", actor=ACTOR)
            await h.advance(5 * ONE_MINUTE)
            assert h.run("load").state is RunState.SUCCEEDED

        h.run_engine(first)
        again = restarted(h, FakeClock(START + 10 * ONE_MINUTE))

        async def second() -> None:
            """Rerun ``load`` and check its environment."""
            await again.engine.rerun(load_id, actor=ACTOR)
            await again.advance(ONE_MINUTE)
            (spec,) = specs_for(again, "load")
            assert spec.env["PYSCHED_FILES"] == path

        again.run_engine(second)

    def test_two_watchers_in_one_poll_both_reach_the_dependant(
        self, harness: MakeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two watchers succeeding at the same poll both land in ``d``'s PYSCHED_FILES.

        ``b``'s look is a little late and every commit with files is slow,
        so ``a``'s commit lands while ``b`` is in the book but not yet saved:
        ``d`` must still see ``b``'s file.
        """
        real_stat = watcher.stat_matches

        def late_stat(pattern: str) -> dict[str, watcher.Signature]:
            """Look as usual, ``b`` a little late."""
            if pattern.endswith("b.csv"):
                time.sleep(LATE_LOOK)
            return real_stat(pattern)

        monkeypatch.setattr(watcher, "stat_matches", late_stat)
        paths = [str(tmp_path / f"{name}.csv") for name in ("a", "b")]
        for path in paths:
            Path(path).write_text("x", encoding="utf-8")
        watchers = "".join(
            f"  {name}:\n    type: file_watcher\n    schedule: manual\n"
            f"    watch: '{path}'\n    min_age: 0s\n"
            for name, path in zip(("a", "b"), paths, strict=True)
        )
        h = harness(
            "jobs:\n" + watchers + "  d:\n    command: 'echo {files}'\n    depends_on: [a, b]\n"
        )
        real_apply = h.store.apply

        def slow_apply(batch: Batch) -> None:
            """Commit as usual, slowly when the batch carries a watcher's files."""
            if batch.run_files:
                time.sleep(SLOW_COMMIT)
            real_apply(batch)

        monkeypatch.setattr(h.store, "apply", slow_apply)

        async def body() -> None:
            """Trigger both watchers and let them succeed together."""
            await pynet.loop.gather(
                h.engine.trigger("a", actor=ACTOR), h.engine.trigger("b", actor=ACTOR)
            )
            await h.advance(ONE_MINUTE)
            (spec,) = specs_for(h, "d")
            assert sorted(spec.env["PYSCHED_FILES"].splitlines()) == paths

        h.run_engine(body)


class TestSlots:
    """Watchers wait without holding the global slot."""

    def test_watcher_takes_no_global_slot(self, harness: MakeHarness, tmp_path: Path) -> None:
        """With max_running 1, three polling watchers leave room for a command job."""
        jobs = "".join(
            f"  w{n}:\n    type: file_watcher\n    schedule: manual\n"
            f"    watch: '{tmp_path}/none{n}.csv'\n"
            for n in range(WATCHERS)
        )
        h = harness(
            f"jobs:\n{jobs}  c:\n    command: work\n    schedule: manual\n",
            settings="max_running: 1",
        )

        async def body() -> None:
            """Start the watchers, then the command job."""
            for n in range(WATCHERS):
                await h.engine.trigger(f"w{n}", actor=ACTOR)
            await h.advance(POLL)
            await h.engine.trigger("c", actor=ACTOR)
            await h.engine.settle()
            assert h.run("c").state is RunState.RUNNING
            assert all(h.run(f"w{n}").state is RunState.RUNNING for n in range(WATCHERS))
            assert h.engine.admission.running == 1

        h.run_engine(body)


class TestEndings:
    """Giving up, retrying and being killed."""

    def test_watcher_gives_up_and_alerts(self, tmp_path: Path) -> None:
        """No file in 1m: outcome timeout, run failed, ``watcher_timeout`` and ``failed`` raised."""
        h = monitored(tmp_path, watcher_config(tmp_path, give_up="1m"))

        async def body(monitor: AlertMonitor) -> None:
            """Wait past the deadline and read the alerts."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await step(h, monitor, START + 2 * ONE_MINUTE)
            (row,) = await attempts(h, run.run_id)
            assert row.outcome is Outcome.TIMEOUT
            assert row.reason == GAVE_UP
            assert row.ended_at == START + ONE_MINUTE
            assert h.run("in").state is RunState.FAILED
            assert h.run("load").state is RunState.BLOCKED
            ids = {a.alert_id for a in await raised(h, monitor)}
            assert f"{run.run_id}:watcher_timeout:1" in ids
            assert f"{run.run_id}:failed:1" in ids
            (timeout,) = await raised(h, monitor, AlertKind.WATCHER_TIMEOUT)
            assert GAVE_UP in timeout.message

        run_monitor(h, body)

    def test_watcher_retry(self, harness: MakeHarness, tmp_path: Path) -> None:
        """The second attempt finds the file created after the first gave up."""
        retries = "retries: {count: 1, delay: 1m}"
        h = harness(watcher_config(tmp_path, give_up="1m", extra=retries))

        async def body() -> None:
            """Give up once, create the file, then succeed on the retry."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await h.advance(ONE_MINUTE)
            assert h.run("in").state is RunState.RETRY_WAIT
            (tmp_path / FILE_NAME).write_text("x", encoding="utf-8")
            await h.advance(2 * ONE_MINUTE)
            rows = await attempts(h, run.run_id)
            assert [r.outcome for r in rows] == [Outcome.TIMEOUT, Outcome.SUCCEEDED]
            assert rows[1].ended_at == START + 2 * ONE_MINUTE + MIN_AGE
            assert h.run("in").state is RunState.SUCCEEDED

        h.run_engine(body)

    def test_kill_watcher(self, harness: MakeHarness, tmp_path: Path) -> None:
        """An operator kill ends the watcher failed, ``killed by alice``."""
        h = harness(watcher_config(tmp_path))

        async def body() -> None:
            """Kill the polling watcher."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await h.advance(POLL)
            await h.engine.kill(run.run_id, actor=ACTOR)
            await h.engine.settle()
            ended = h.run("in")
            assert ended.state is RunState.FAILED
            assert ended.reason == f"killed by {ACTOR}"
            (row,) = await attempts(h, run.run_id)
            assert row.outcome is Outcome.KILLED
            assert h.engine.admission.running == 0

        h.run_engine(body)


class TestRestart:
    """A watcher polling when the scheduler stops resumes when it starts again."""

    def test_restart_resumes_watcher(self, harness: MakeHarness, tmp_path: Path) -> None:
        """Stopped at +40s of a 1m give-up, it gives up at +1m (timeout, not lost)."""
        h = harness(watcher_config(tmp_path, give_up="1m"))
        run_id = "in.2026-10-19.1"

        async def first() -> None:
            """Start the watcher and stop 40 s in."""
            await h.engine.trigger("in", actor=ACTOR)
            await h.advance_to(STOPPED_AT)
            assert h.run("in").state is RunState.RUNNING

        h.run_engine(first)
        again = restarted(h, FakeClock(STOPPED_AT))

        async def second() -> None:
            """Let the resumed watcher reach its original deadline."""
            assert again.run("in").state is RunState.RUNNING
            assert again.engine.admission.running == 0
            await again.advance(2 * ONE_MINUTE)
            (row,) = await attempts(again, run_id)
            assert row.outcome is Outcome.TIMEOUT
            assert row.reason == GAVE_UP
            assert row.started_at == START
            assert row.ended_at == START + ONE_MINUTE
            assert again.run("in").state is RunState.FAILED

        again.run_engine(second)


class TestWatchLimit:
    """The shared limit on looks at the disk follows ``scheduler.watch_limit``."""

    def test_reload_rebuilds_watch_limit(self, harness: MakeHarness, tmp_path: Path) -> None:
        """A reload that changes the size builds a new semaphore; the same size keeps it."""
        h = harness(watcher_config(tmp_path), settings="watch_limit: 2")

        async def body() -> None:
            """Reload twice: once with the size changed, once with only a new digest."""
            first = h.engine.watch_limit
            same = replace(h.config, digest="same-size")
            await h.engine.reload(same, actor=ACTOR)
            assert h.engine.watch_limit is first
            bigger = replace(
                h.config, settings=replace(h.config.settings, watch_limit=4), digest="bigger"
            )
            await h.engine.reload(bigger, actor=ACTOR)
            assert h.engine.watch_limit is not first

        h.run_engine(body)


class TestFixRound:
    """Files of old dates, stale files after a rerun, and watchers at fail-stop."""

    def test_old_date_rerun_gets_files(self, harness: MakeHarness, tmp_path: Path) -> None:
        """A downstream run older than memory_days, rerun after a restart, gets the files."""
        h = harness(watcher_config(tmp_path), settings="memory_days: 3")
        old = TODAY - timedelta(days=OLD_DAYS)
        path = str(tmp_path / f"in_{old:%Y%m%d}.csv")
        Path(path).write_text("x", encoding="utf-8")
        watcher_id, load_id = f"in.{old}.1", f"load.{old}.1"

        async def first() -> None:
            """Run the old date's watcher and ``load`` to the end."""
            await h.engine.trigger("in", run_date=old, actor=ACTOR)
            await h.advance(5 * ONE_MINUTE)
            assert h.engine.book.get(load_id) is not None

        h.run_engine(first)
        again = restarted(h, FakeClock(START + 10 * ONE_MINUTE))

        async def second() -> None:
            """Rerun the old ``load``: its upstream comes back with its files."""
            assert again.engine.book.get(watcher_id) is None
            await again.engine.rerun(load_id, actor=ACTOR)
            await again.advance(ONE_MINUTE)
            assert again.engine.files_for(watcher_id) == (path,)
            (spec,) = specs_for(again, "load")
            assert spec.env["PYSCHED_FILES"] == path

        again.run_engine(second)

    def test_watcher_rerun_clears_files(self, harness: MakeHarness, tmp_path: Path) -> None:
        """Rerunning a succeeded watcher forgets its old files, in memory and in the store."""
        h = harness(watcher_config(tmp_path))
        target = tmp_path / FILE_NAME
        target.write_text("x", encoding="utf-8")

        async def body() -> None:
            """Succeed, delete the file, rerun, and look for the old files."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await h.advance(ONE_MINUTE)
            assert h.engine.files_for(run.run_id) == (str(target),)
            target.unlink()
            await h.engine.rerun(run.run_id, actor=ACTOR)
            await h.engine.settle()
            assert h.engine.files_for(run.run_id) == ()
            stored = await pynet.loop.run_in_thread(h.store.run_files, [run.run_id])
            assert stored.get(run.run_id, ()) == ()

        h.run_engine(body)

    def test_fail_stop_stops_watchers(self, harness: MakeHarness, tmp_path: Path) -> None:
        """After a fail-stop a watcher looks no more; its run stays running for the restart."""
        h = harness(watcher_config(tmp_path))

        async def body() -> None:
            """Fail the engine, then create the file and wait well past min_age."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await h.advance(POLL)
            h.engine.fail_stop(RuntimeError("boom"))
            await h.engine.settle()
            (tmp_path / FILE_NAME).write_text("x", encoding="utf-8")
            await h.advance(5 * ONE_MINUTE)
            stored = await pynet.loop.run_in_thread(h.store.get_run, run.run_id)
            assert stored is not None
            assert stored.state is RunState.RUNNING
            (row,) = await attempts(h, run.run_id)
            log = Path(row.stdout_path)
            assert not log.exists() or "found" not in log.read_text(encoding="utf-8")

        h.run_engine(body)
