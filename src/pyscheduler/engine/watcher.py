"""Watch for files: poll a glob until a match has sat still long enough.

A file watcher job has no process. It is like waiting at the window for a
parcel: you look out every so often, and you only trust that the parcel is
really there once it has not moved for a while (a file still being copied
keeps changing its size, so it is not ready yet).

``WatcherHandle`` looks like any other ``AttemptHandle`` to the engine, so
the engine can ``wait()``, ``terminate()`` and ``kill()`` it the same way.
The ``stat`` calls run on a helper thread, and a shared ``Semaphore`` caps
how many watchers look at the disk at once.
"""

import glob
import logging
import os
import signal
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop
from pynet.loop import Cancelled, Semaphore, Task

from pyscheduler.config.model import WatchSpec
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.status_note import AttemptResult

__all__ = ["WatcherHandle", "stat_matches"]

logger = logging.getLogger("pyscheduler.engine.watcher")

NANOSECONDS = 1_000_000_000
"""Nanoseconds in a second (``st_mtime_ns`` counts in them)."""

type Signature = tuple[int, int, int]
"""A file's ``(size, mtime_ns, inode)``: if it is unchanged the file is still.

The inode catches a file swapped for another of the same size and time.
"""


def stat_matches(pattern: str) -> dict[str, Signature]:
    """Map each regular file matching ``pattern`` to its ``(size, mtime_ns, inode)``.

    This blocks, so call it on a helper thread. Directories are ignored and
    files that vanish between the glob and the stat are skipped.
    """
    found: dict[str, Signature] = {}
    for path in glob.glob(pattern):  # noqa: PTH207 -- the pattern is a plain string
        try:
            info = os.stat(path)  # noqa: PTH116
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found[path] = (info.st_size, info.st_mtime_ns, info.st_ino)
    return found


def _modified(sig: Signature, now: datetime) -> datetime:
    """Return when the file of ``sig`` last changed, as UTC (never later than ``now``)."""
    changed = datetime.fromtimestamp(sig[1] / NANOSECONDS, tz=UTC)
    return min(changed, now)


def _append(path: Path, text: str) -> None:
    """Append ``text`` to ``path``, creating its folders (runs on a helper thread)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


class WatcherHandle:
    """An attempt that polls for stable files instead of running a process.

    Args:
        pattern: The glob to watch.
        spec: How still a file must be, how often to look and when to quit.
        started_at: When the attempt began; the give-up deadline counts from here.
        clock: The engine's clock.
        limit: Caps how many watchers stat the disk at once.
        log_path: The file the watcher appends its progress lines to.
        busy: Called with ``+1`` when a look at the disk begins and ``-1``
            when it is over (the watcher sleeps or ends), so the engine
            can tell when it is quiet.
        resumed: Whether this attempt was polling before a restart. Its
            first look then counts each match as still since its
            modification time: nobody was watching meanwhile.

    Attributes:
        files: The stable matches, sorted; set when the watch succeeds.
        gave_up: Whether the watch ended at its deadline with nothing stable.

    """

    def __init__(
        self,
        *,
        pattern: str,
        spec: WatchSpec,
        started_at: datetime,
        clock: Clock,
        limit: Semaphore,
        log_path: Path,
        busy: Callable[[int], None] | None = None,
        resumed: bool = False,
    ) -> None:
        """Remember what to watch; polling starts when ``wait`` is called."""
        self._pattern = pattern
        self._spec = spec
        self._started_at = started_at
        self._clock = clock
        self._limit = limit
        self._log_path = log_path
        self._busy = busy
        self._resumed = resumed
        self._looking = False
        self._task: Task[AttemptResult] | None = None
        self._stopped = False
        self.files: tuple[str, ...] = ()
        self.gave_up = False

    @property
    def pid(self) -> int | None:
        """Return ``None``: a watcher has no process."""
        return None

    @property
    def spec(self) -> WatchSpec:
        """Return what this watcher waits for."""
        return self._spec

    @property
    def log_path(self) -> Path:
        """Return the file the watcher appends its progress lines to."""
        return self._log_path

    def _look(self, *, looking: bool) -> None:
        """Tell the ``busy`` callback when a look at the disk begins or ends."""
        if looking != self._looking:
            self._looking = looking
            if self._busy is not None:
                self._busy(1 if looking else -1)

    def _result(self, *, exit_code: int | None, sig: int | None) -> AttemptResult:
        """Build the result for how the watch ended, stamped with the current time."""
        return AttemptResult(exit_code, sig, self._started_at, self._clock.now(), None)

    async def wait(self) -> AttemptResult:
        """Poll until a file is stable, the deadline passes, or the watch is stopped."""
        if self._stopped:
            return self._result(exit_code=None, sig=signal.SIGTERM)
        if self._task is None:
            self._task = pynet.loop.spawn(self._poll(), name="watcher-poll")
        try:
            return await self._task
        except Cancelled:
            if not self._stopped:
                self._task.cancel()
                raise
            return self._result(exit_code=None, sig=signal.SIGTERM)

    def terminate(self) -> None:
        """Stop polling; ``wait`` then returns a SIGTERM result."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()

    def kill(self) -> None:
        """Stop polling at once; the same as ``terminate`` for a watcher."""
        self.terminate()

    async def _write(self, text: str) -> None:
        """Append ``text`` to the log on a helper thread; a failing log never stops the watch."""
        try:
            await pynet.loop.run_in_thread(_append, self._log_path, text)
        except OSError:
            logger.warning("cannot write watcher log %s", self._log_path, exc_info=True)

    async def _poll(self) -> AttemptResult:
        """Look every ``poll`` until success or the deadline; return how it ended."""
        self._look(looking=True)
        try:
            return await self._polls()
        finally:
            self._look(looking=False)

    async def _polls(self) -> AttemptResult:
        """Run the polls for ``_poll`` (which tells ``busy`` when they start and end).

        A file counts as stable once it has been still for ``min_age`` by
        the time of the look, or, for a watcher resumed after its deadline,
        by the deadline itself (a file that came later came too late).
        """
        deadline = self._started_at + self._spec.give_up_after
        min_age = self._spec.min_age
        first_seen: dict[str, tuple[Signature, datetime]] = {}
        last: dict[str, Signature] = {}
        resumed = self._resumed
        while True:
            async with self._limit:
                matches = await pynet.loop.run_in_thread(stat_matches, self._pattern)
            now = self._clock.now()
            first_seen = {
                path: first_seen[path]
                if path in first_seen and first_seen[path][0] == sig
                else (sig, _modified(sig, now) if resumed else now)
                for path, sig in matches.items()
            }
            resumed = False
            cutoff = min(now, deadline)
            stable = sorted(
                path for path, (_, since) in first_seen.items() if cutoff - since >= min_age
            )
            lines = [f"{now:%H:%M:%S} {len(matches)} match(es)\n"] if matches != last else []
            last = matches
            if stable:
                lines += [f"found {path}\n" for path in stable]
            if lines:
                await self._write("".join(lines))
            if stable:
                self.files = tuple(stable)
                return self._result(exit_code=0, sig=None)
            if now >= deadline:
                break
            self._look(looking=False)
            await self._clock.sleep_until(min(now + self._spec.poll, deadline))
            self._look(looking=True)
        self.gave_up = True
        return self._result(exit_code=None, sig=None)
