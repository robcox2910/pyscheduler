"""File-watcher runs in the engine: build their watchers and hand their files on.

A watcher run is a friend waiting at the letterbox. It holds no place on
the trampoline (no global slot), only one of the ``watch_limit`` pairs of
eyes that may look at the disk at once. When the parcel has stopped
rustling, the friend writes down which parcels came (the run's files), and
every job waiting on that friend is handed the list: as ``PYSCHED_FILES``
(one path per line) and as the ``{files}`` blank (quoted for the shell).
"""

import shlex
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from pynet.loop import Semaphore

from pyscheduler.config import JobDef
from pyscheduler.config.job_fields import DEFAULT_STDOUT
from pyscheduler.config.model import JobKind, WatchSpec
from pyscheduler.engine.base import EngineBase
from pyscheduler.engine.model import Run
from pyscheduler.engine.watcher import WatcherHandle
from pyscheduler.templates import render
from pyscheduler.values import format_duration

__all__ = ["WatchRuns", "give_up_reason"]


def give_up_reason(handle: WatcherHandle) -> str:
    """Return the reason on an attempt whose watcher gave up (ruling P2-R4)."""
    return f"gave up: no stable file after {format_duration(handle.spec.give_up_after)}"


class WatchRuns(EngineBase):
    """Give the engine file-watcher attempts and the files they found.

    Attributes:
        watch_limit: Caps how many watchers look at the disk at once
            (``scheduler.watch_limit``); set up by ``Engine``.

    """

    watch_limit: Semaphore
    _files: dict[str, tuple[str, ...]]
    """The files each succeeded watcher run found, by run id (set up by ``Engine``)."""
    _watch_size: int
    """The size ``watch_limit`` was built with (set up by ``Engine``)."""

    def files_for(self, run_id: str) -> tuple[str, ...]:
        """Return the files the watcher run ``run_id`` found, or ``()`` if none are known."""
        return self._files.get(run_id, ())

    def _values(self, run: Run, job: JobDef) -> Mapping[str, object]:
        """Return the values the templates of ``run``'s current attempt are filled from."""
        return {
            "job": job.name,
            "run_date": run.run_date,
            "run_id": run.run_id,
            "attempt": run.attempt,
            "scheduled_at": run.scheduled_at,
            "log_dir": str(self.config.settings.log_dir),
            "files": shlex.join(self._upstream_files(run, job) or ()),
        }

    def _upstream_files(self, run: Run, job: JobDef) -> tuple[str, ...] | None:
        """Return the files of ``run``'s direct watcher upstreams, or ``None`` if it has none.

        Each upstream run is the one the dependency looks at: the latest
        run of the upstream job for the run date shifted by the
        dependency's offset on ``job``'s calendar (as ``deps`` does).
        """
        calendar = self.config.calendar_for(job)
        found: list[str] | None = None
        for dep in job.depends_on:
            upstream_job = self.config.jobs.get(dep.job)
            if upstream_job is None or upstream_job.kind is not JobKind.FILE_WATCHER:
                continue
            found = found or []
            try:
                day = calendar.shift(run.run_date, dep.run_date_offset)
            except ValueError:
                continue
            upstream = self.book.latest_for(dep.job, day)
            if upstream is not None:
                found.extend(self._files.get(upstream.run_id, ()))
        return None if found is None else tuple(found)

    def _watch_log(self, run: Run, job: JobDef) -> Path:
        """Return the log a watcher attempt writes to: the default stdout path.

        Raises:
            KeyError: If a template names an unknown value.
            ValueError: If a template is malformed.

        """
        return Path(render(DEFAULT_STDOUT, self._values(run, job)))

    def _watcher(
        self, run: Run, job: JobDef, watch: WatchSpec, started_at: datetime, log: Path
    ) -> WatcherHandle:
        """Build the watcher of ``run``'s attempt (``job``'s ``watch``), timed from ``started_at``.

        Raises:
            KeyError: If the ``watch`` pattern names an unknown value.
            ValueError: If the pattern is malformed.

        """
        return WatcherHandle(
            pattern=render(watch.pattern, self._values(run, job)),
            spec=watch,
            started_at=started_at,
            clock=self.clock,
            limit=self.watch_limit,
            log_path=log,
            busy=self._poll_busy,
        )

    def _fail(self, exc: BaseException, run_ids: Iterable[str]) -> None:
        """Fail stop as ``EngineBase._fail`` does, and stop every polling watcher at once.

        A watcher has no process to leave running, so its attempt task is
        cancelled (as at ``stop``): nothing is recorded, its ``watch_limit``
        permit is given back, and the next start resumes it.
        """
        first = self.error is None
        watching = [
            task
            for run_id, handle in self._handles.items()
            if isinstance(handle, WatcherHandle)
            and (task := self._attempt_tasks.get(run_id)) is not None
        ]
        super()._fail(exc, run_ids)
        if first:
            for task in watching:
                task.cancel()

    def _poll_busy(self, delta: int) -> None:
        """Count a watcher's look at the disk as engine work (``settle`` waits for it)."""
        self._busy += delta
        self._polls += delta

    def _resize_watch_limit(self) -> None:
        """Build a new ``watch_limit`` semaphore if a reload changed its size.

        Watchers already polling keep the old one until they end.
        """
        size = self.config.settings.watch_limit
        if size != self._watch_size:
            self._watch_size = size
            self.watch_limit = Semaphore(size)
