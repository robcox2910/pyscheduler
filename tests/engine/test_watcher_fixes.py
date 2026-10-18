"""Test the watcher fixes: files of two watchers, resuming after downtime, reloads, inodes."""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pynet.loop
import pytest
from engine_helpers import START, TODAY, MakeHarness
from pynet.loop import Semaphore
from watcher_helpers import attempts, restarted, specs_for, watcher_config

from pyscheduler.config import load_config
from pyscheduler.config.model import WatchSpec
from pyscheduler.engine import watcher
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.model import RunState
from pyscheduler.engine.reload import DISABLED, REMOVED
from pyscheduler.engine.watcher import WatcherHandle
from pyscheduler.store import Batch

ACTOR = "alice"
ONE_MINUTE = timedelta(minutes=1)
MIN_AGE = timedelta(seconds=30)
POLL = timedelta(seconds=10)
STOPPED_AT = START + timedelta(seconds=40)
FILE_NAME = "in_20261019.csv"
LATE_LOOK = 0.1
SLOW_COMMIT = 0.3
SETTLE_NAP = 0.002
NANOSECONDS = 1_000_000_000

RELOAD_BEFORE = """\
jobs:
  in: {type: file_watcher, watch: 'PATH', schedule: manual}
  other: {command: 'true', schedule: manual}
"""


def set_mtime(path: Path, when: datetime) -> None:
    """Give ``path`` the modification time ``when``."""
    stamp = int(when.timestamp() * NANOSECONDS)
    os.utime(path, ns=(stamp, stamp))


def handle_for(
    clock: FakeClock, tmp_path: Path, *, started_at: datetime, resumed: bool
) -> WatcherHandle:
    """Build a watcher of ``tmp_path/*.csv`` (min_age 30s, poll 10s, give up after 1m)."""
    return WatcherHandle(
        pattern=str(tmp_path / "*.csv"),
        spec=WatchSpec(str(tmp_path / "*.csv"), MIN_AGE, POLL, ONE_MINUTE),
        started_at=started_at,
        clock=clock,
        limit=Semaphore(1),
        log_path=tmp_path / "logs" / "watch.log",
        resumed=resumed,
    )


class TestFiles:
    """Matched files reach the dependant even when watchers finish together."""

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


class TestResume:
    """A watcher resumed after downtime counts the time nobody was watching (FR7)."""

    def test_resumed_after_the_deadline_finds_a_file_that_came_in_time(
        self, tmp_path: Path
    ) -> None:
        """Succeed at once: the file has not changed since well before the deadline."""
        target = tmp_path / "in.csv"
        target.write_text("x", encoding="utf-8")
        set_mtime(target, START + timedelta(seconds=10))  # deadline 1m, min_age 30s

        async def main() -> None:
            """Resume ten minutes after the start."""
            clock = FakeClock(START + 10 * ONE_MINUTE)
            handle = handle_for(clock, tmp_path, started_at=START, resumed=True)
            assert (await handle.wait()).succeeded
            assert handle.files == (str(target),)

        pynet.loop.run(main())

    def test_resumed_after_the_deadline_gives_up_on_a_late_file(self, tmp_path: Path) -> None:
        """Give up: the file was not still for min_age by the deadline."""
        target = tmp_path / "in.csv"
        target.write_text("x", encoding="utf-8")
        set_mtime(target, START + timedelta(seconds=50))

        async def main() -> None:
            """Resume ten minutes after the start."""
            clock = FakeClock(START + 10 * ONE_MINUTE)
            handle = handle_for(clock, tmp_path, started_at=START, resumed=True)
            await handle.wait()
            assert handle.gave_up

        pynet.loop.run(main())

    def test_engine_resumes_a_watcher_past_its_deadline(
        self, harness: MakeHarness, tmp_path: Path
    ) -> None:
        """A file that came while the scheduler was down, in time, ends the watch succeeded."""
        h = harness(watcher_config(tmp_path, give_up="1m"))
        path = tmp_path / FILE_NAME

        async def first() -> None:
            """Start the watcher and stop 40 s in, with nothing there yet."""
            await h.engine.trigger("in", actor=ACTOR)
            await h.advance_to(STOPPED_AT)
            assert h.run("in").state is RunState.RUNNING

        h.run_engine(first)
        path.write_text("a,b\n", encoding="utf-8")
        set_mtime(path, START + timedelta(seconds=20))
        again = restarted(h, FakeClock(START + 10 * ONE_MINUTE))

        async def second() -> None:
            """Let the resumed watcher look once, and ``load`` start."""
            await again.advance(ONE_MINUTE)
            assert again.run("in").state is RunState.SUCCEEDED
            (spec,) = specs_for(again, "load")
            assert spec.env["PYSCHED_FILES"] == str(path)

        again.run_engine(second)


class TestSignature:
    """A file swapped for another of the same size and time is not still."""

    def test_replaced_file_starts_over(self, tmp_path: Path) -> None:
        """Count min_age again from the swap, as the inode changed."""
        target = tmp_path / "in.csv"
        target.write_text("x", encoding="utf-8")
        stamp = target.stat().st_mtime_ns

        async def main() -> None:
            """Swap the file before the poll at +10s; it is found at +40s, not +30s."""
            clock = FakeClock(START)
            handle = handle_for(clock, tmp_path, started_at=START, resumed=False)
            task = pynet.loop.spawn(handle.wait())
            while clock.next_due() is None:
                await pynet.loop.sleep(SETTLE_NAP)
            other = tmp_path / "other.tmp"
            other.write_text("y", encoding="utf-8")
            os.utime(other, ns=(stamp, stamp))
            other.replace(target)
            while not task.done():
                due = clock.next_due()
                if due is None:
                    await pynet.loop.sleep(SETTLE_NAP)
                    continue
                await clock.advance_to(due)
            assert (await task).succeeded
            assert clock.now() == START + POLL + MIN_AGE

        pynet.loop.run(main())


class TestReloadStopsWatchers:
    """A reload stops watchers whose job is gone, disabled or no longer a watcher (FR8)."""

    @pytest.mark.parametrize(
        ("after", "reason"),
        [
            ("jobs:\n  other: {command: 'true', schedule: manual}\n", REMOVED),
            (
                RELOAD_BEFORE.replace(
                    "schedule: manual}\n  other", "schedule: manual, enabled: false}\n  other"
                ),
                DISABLED,
            ),
            (
                "jobs:\n  in: {command: 'true', schedule: manual}\n  other: {command: 'true', schedule: manual}\n",
                REMOVED,
            ),
        ],
        ids=["removed", "disabled", "now-a-command"],
    )
    def test_reload_cancels_the_watcher(
        self, harness: MakeHarness, tmp_path: Path, after: str, reason: str
    ) -> None:
        """End the polling watcher's run cancelled for the reload's reason; launch nothing."""
        h = harness(RELOAD_BEFORE.replace("PATH", str(tmp_path / "none.csv")))
        path = tmp_path / "after.yaml"
        header = (tmp_path / "pysched.yaml").read_text(encoding="utf-8").split("jobs:\n")[0]
        path.write_text(
            header + after.replace("PATH", str(tmp_path / "none.csv")), encoding="utf-8"
        )
        new_config = load_config(path, env={}, today=TODAY)

        async def body() -> None:
            """Start the watcher, reload, and check the run and the attempt."""
            run = await h.engine.trigger("in", actor=ACTOR)
            await h.advance(POLL)
            assert h.run("in").state is RunState.RUNNING
            await h.engine.reload(new_config, actor=ACTOR)
            await h.advance(POLL)
            ended = h.engine.book.get(run.run_id)
            assert ended is not None
            assert (ended.state, ended.reason) == (RunState.CANCELLED, reason)
            (row,) = await attempts(h, run.run_id)
            assert row.reason == reason
            assert h.launcher.launched == []

        h.run_engine(body)
