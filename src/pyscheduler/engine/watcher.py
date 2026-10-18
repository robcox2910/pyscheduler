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
from datetime import datetime
from pathlib import Path

import pynet.loop
from pynet.loop import Cancelled, Semaphore, Task

from pyscheduler.config.model import WatchSpec
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.status_note import AttemptResult

__all__ = ["WatcherHandle", "stat_matches"]

logger = logging.getLogger("pyscheduler.engine.watcher")

type Signature = tuple[int, int]
"""A file's ``(size, mtime_ns)``: if it is unchanged the file is still."""


def stat_matches(pattern: str) -> dict[str, Signature]:
    """Map each regular file matching ``pattern`` to its ``(size, mtime_ns)``.

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
            found[path] = (info.st_size, info.st_mtime_ns)
    return found


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
    ) -> None:
        """Remember what to watch; polling starts when ``wait`` is called."""
        self._pattern = pattern
        self._spec = spec
        self._started_at = started_at
        self._clock = clock
        self._limit = limit
        self._log_path = log_path
        self._task: Task[AttemptResult] | None = None
        self._stopped = False
        self.files: tuple[str, ...] = ()
        self.gave_up = False

    @property
    def pid(self) -> int | None:
        """Return ``None``: a watcher has no process."""
        return None

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
        deadline = self._started_at + self._spec.give_up_after
        first_seen: dict[str, tuple[Signature, datetime]] = {}
        last: dict[str, Signature] = {}
        while True:
            async with self._limit:
                matches = await pynet.loop.run_in_thread(stat_matches, self._pattern)
            now = self._clock.now()
            first_seen = {
                path: first_seen[path]
                if path in first_seen and first_seen[path][0] == sig
                else (sig, now)
                for path, sig in matches.items()
            }
            stable = sorted(
                path for path, (_, since) in first_seen.items() if now - since >= self._spec.min_age
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
            await self._clock.sleep_until(min(now + self._spec.poll, deadline))
        self.gave_up = True
        return self._result(exit_code=None, sig=None)
