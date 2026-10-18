"""The engine's shared state and its one rule: write it down, then act.

Every change to a run follows the same three steps, like a teacher who
writes a mark in the register *before* telling the class:

1. put the new run in the run book (memory), so the rest of the engine
   sees it at once and never starts the same run twice;
2. ``await`` the store writer, so the change is durable on disk;
3. publish the run (the API's view) and tell the subscribers.

Only then may the engine act on it, for example start a process.

``EngineBase`` holds the state the other engine parts share: the config,
the run book, the slots, the store writer and the staging tray. The
synchronous parts of the engine (creating runs, re-checking
dependencies) only *stage* their changes; ``_flush`` commits the tray.

If a commit ever fails, the engine **fails stop**: it records the error
(``error``, and ``wait_failed()`` returns it), stops creating and
starting runs, launches nothing more, and puts the runs whose change was
lost back to their last durable copy. Recovery on the next start
reconciles the rest from the store.
"""

import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Any

import pynet.loop
from pynet.loop import Event as LoopEvent
from pynet.loop import Task

from pyscheduler.config import Config, JobDef
from pyscheduler.engine.admission import Admission, ReadyQueue
from pyscheduler.engine.clock import Cancel, Clock
from pyscheduler.engine.launcher import AttemptHandle, Launcher
from pyscheduler.engine.locks import RunLocks
from pyscheduler.engine.model import Attempt, Event, Run, RunState
from pyscheduler.engine.runbook import RunBook
from pyscheduler.store import Batch, ConfigRecord, Store, StoreWriter

ACTOR = "scheduler"
"""The actor named on events the engine itself records."""

SETTLE_LIMIT = 10_000
"""The most loop turns ``settle`` waits before deciding something is stuck."""

IDLE_TURNS = 3
"""How many quiet loop turns in a row ``settle`` needs before it believes it."""

WRITER_NAP = 0.001
"""How long (in real seconds) ``settle`` naps while a commit runs on its thread."""

logger = logging.getLogger("pyscheduler.engine")


class EngineFailedError(RuntimeError):
    """Raised inside the engine after a failed commit; the failure is already recorded."""


class EngineBase(ABC):
    """Hold the engine's shared state and commit changes write-ahead.

    Attributes:
        config: The config in force.
        book: Every run in memory, with its published (durable) view.
        admission: The global cap and pool counters.
        ready: The queued runs waiting for a slot.
        writer: The group-commit store writer.
        error: The error that made the engine fail stop, or ``None``.

    """

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        clock: Clock,
        launcher: Launcher,
        config_version: int = 1,
    ) -> None:
        """Set up empty state; nothing runs until ``start``."""
        self.config = config
        self.store = store
        self.clock = clock
        self.launcher = launcher
        self.config_version = config_version
        self.book = RunBook()
        self.admission = Admission(config.settings.max_running, config.pools)
        self.ready = ReadyQueue()
        self.writer = StoreWriter(store)
        self._subscribers: list[Callable[[Run], None]] = []
        self._staged: dict[str, None] = {}
        self._staged_events: list[Event] = []
        self._flush_planned = False
        self._busy = 0
        self._tasks: set[Task[None]] = set()
        self._attempt_tasks: dict[str, Task[None]] = {}
        self._handles: dict[str, AttemptHandle] = {}
        self._needs: dict[str, Mapping[str, int]] = {}
        self._force: set[str] = set()
        self._kill_requested: set[str] = set()
        self._kill_actors: dict[str, str] = {}
        self._kill_timers: dict[str, Cancel] = {}
        self._retry_timers: dict[str, Cancel] = {}
        self._retention_timer: Cancel | None = None
        self._run_locks = RunLocks()
        self._held: set[str] = set()
        self._stopping = False
        self._recently_deleted: set[str] = set()
        """Run ids the current (or last) prune round deleted; cleared each round."""
        self._reach_for: Config | None = None
        self._reach = 0
        """Days the dependency offsets of ``_reach_for`` look back (see ``_memory_start``)."""
        self.closing = threading.Event()
        """Set by ``stop`` and fail-stop: the launch thread forks nothing more."""
        self.error: BaseException | None = None
        self._failed = LoopEvent()

    # -- subscribers and settling ----------------------------------------

    def subscribe(self, callback: Callable[[Run], None]) -> Callable[[], None]:
        """Call ``callback`` with each run once its change is durable; return an unsubscribe."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            """Stop calling ``callback`` (a second call does nothing)."""
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    async def settle(self) -> None:
        """Wait until the writer is idle and no transition work is pending (a test helper).

        Raises:
            RuntimeError: If the engine is still busy after ``SETTLE_LIMIT`` turns.

        """
        quiet = 0
        for _ in range(SETTLE_LIMIT):
            if self._busy == 0 and self.writer.idle and not self._staged:
                quiet += 1
                if quiet >= IDLE_TURNS:
                    return
                await pynet.loop.sleep(0)
            else:
                quiet = 0
                await pynet.loop.sleep(0 if self.writer.idle else WRITER_NAP)
        msg = "engine did not settle"
        raise RuntimeError(msg)

    async def wait_failed(self) -> BaseException:
        """Wait until the engine fails stop, and return the error that caused it."""
        await self._failed.wait()
        if self.error is None:  # pragma: no cover -- the event is only set with an error
            msg = "engine failed without an error"
            raise RuntimeError(msg)
        return self.error

    # -- failing stop ----------------------------------------------------

    def _fail(self, exc: BaseException, run_ids: Iterable[str]) -> None:
        """Record ``exc`` as fatal, stop starting work, and roll back ``run_ids``.

        Each run goes back to its published (durable) copy, or is
        forgotten if it never had one; its slots are released and its
        queue entry and attempt task are dropped. Later failures (other
        saves of the same broken commit) only log and roll back.
        """
        ids = sorted(set(run_ids))
        first = self.error is None
        if first:
            self.error = exc
            self._stopping = True
            self.closing.set()
            self._failed.set()
            for timer in self._retry_timers.values():
                timer.cancel()
            self._retry_timers.clear()
        logger.error(
            "engine %s after a failure (runs %s): %r",
            "failing stop" if first else "already stopped",
            ", ".join(ids) or "none",
            exc,
            exc_info=exc if first else None,  # the traceback once; later ones repeat it
        )
        for run_id in ids:
            published = self.book.published(run_id)
            if published is None:
                self.book.forget(run_id)
            else:
                self.book.put(published)
            self.ready.remove(run_id)
            self._release(run_id)
            self._attempt_tasks.pop(run_id, None)
            self._staged.pop(run_id, None)

    def _release(self, run_id: str) -> None:
        """Give back the slots ``run_id`` holds (if any) and forget its needs."""
        needs = self._needs.pop(run_id, {})
        if run_id in self._held:
            self._held.discard(run_id)
            self.admission.release(needs)

    def _guard(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Wrap a timer callback so an error in it fails the engine instead of escaping."""

        def guarded() -> None:
            """Run the callback, sending any error down the fail path."""
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 -- routed to the fail path
                self._fail(exc, ())

        return guarded

    # -- one change at a time per run ------------------------------------

    def _run_lock(self, run_id: str) -> AbstractAsyncContextManager[None]:
        """Return ``run_id``'s lock: one ending or operator action on a run at a time.

        An attempt's ending holds it across its commit and slot release, and
        every operator action across check, stage, commit and act, so an
        action never sees (or changes) a run halfway through a transition.
        """
        return self._run_locks.hold(run_id)

    # -- write-ahead transitions -----------------------------------------

    async def _transition(
        self,
        changes: list[Run],
        *,
        attempts: Iterable[Attempt] = (),
        events: Iterable[Event] = (),
        engine: Mapping[str, str] | None = None,
        configs: Iterable[ConfigRecord] = (),
    ) -> None:
        """Put ``changes`` in the book, make them durable, publish them, then notify."""
        self._busy += 1
        try:
            for run in changes:
                self.book.put(run)
            batch = Batch(
                runs=list(changes),
                attempts=list(attempts),
                events=list(events),
                engine=dict(engine or {}),
                configs=list(configs),
            )
            ids = [run.run_id for run in changes] + [a.run_id for a in batch.attempts]
            try:
                await self.writer.save(batch)
            except Exception as exc:
                self._fail(exc, ids)
                raise EngineFailedError(str(exc)) from exc
            for run in changes:
                self.book.publish(run)
            for run in changes:
                self._notify(run)
            if self.error is not None:
                # Durable, but the engine failed while this save waited: act on nothing.
                self._fail(self.error, ids)
                raise EngineFailedError(str(self.error))
        finally:
            self._busy -= 1

    def _notify(self, run: Run) -> None:
        """Tell every subscriber about ``run``; one that raises is logged and skipped."""
        for callback in list(self._subscribers):
            try:
                callback(run)
            except Exception:
                logger.exception("subscriber failed for %s", run.run_id)

    def _stage(self, run: Run, *events: Event) -> None:
        """Put ``run`` in the book now and add it to the tray for the next flush."""
        self.book.put(run)
        self._staged[run.run_id] = None
        self._staged_events.extend(events)
        if run.is_final:
            self._staged_events.append(
                self._event(run.run_id, "finished", state=run.state.value, reason=run.reason)
            )
        self._flush_soon()

    async def _flush(
        self,
        *,
        engine: Mapping[str, str] | None = None,
        configs: Iterable[ConfigRecord] = (),
    ) -> None:
        """Commit the tray (with the book's current copy of each run), then start what fits.

        The tray is emptied before the first ``await``, so the order of
        saves always matches the order of the book's changes.
        """
        ids, self._staged = list(self._staged), {}
        events, self._staged_events = self._staged_events, []
        runs = [run for run_id in ids if (run := self.book.get(run_id)) is not None]
        configs = list(configs)
        if runs or events or engine or configs:
            await self._transition(runs, events=events, engine=engine, configs=configs)
        self._pump()

    def _flush_soon(self) -> None:
        """Make sure a flush task will run soon (at most one is planned at a time)."""
        if self._flush_planned:
            return
        self._flush_planned = True

        async def flush() -> None:
            """Flush the tray once this task gets its turn."""
            self._flush_planned = False
            await self._flush()

        self._spawn(flush(), "engine-flush")

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> Task[None]:
        """Start ``coro`` as a task counted as busy until it ends, and keep track of it."""
        self._busy += 1
        task = pynet.loop.spawn(self._guarded(coro), name=name)
        self._tasks.add(task)

        def done(_: object) -> None:
            """Forget the finished task."""
            self._busy -= 1
            self._tasks.discard(task)

        task.add_done_callback(done)
        return task

    async def _guarded(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run ``coro``, sending any error down the fail path (it is already there if failed)."""
        try:
            await coro
        except EngineFailedError:
            pass  # recorded and logged by _fail
        except Exception as exc:  # noqa: BLE001 -- routed to the fail path
            self._fail(exc, ())

    # -- small helpers ---------------------------------------------------

    def _event(self, run_id: str | None, action: str, **detail: object) -> Event:
        """Return a logbook line by the scheduler, now, with ``detail`` as JSON."""
        return Event(self.clock.now(), ACTOR, run_id, action, json.dumps(detail, default=str))

    def _changed(self, run: Run, state: RunState, reason: str = "") -> Run:
        """Return a copy of ``run`` in ``state`` with ``reason``, stamped now."""
        return replace(run, state=state, reason=reason, updated_at=self.clock.now())

    def _queue(self, run: Run, job: JobDef) -> Run:
        """Stage ``run`` as queued and put it in the ready queue."""
        queued = self._changed(run, RunState.QUEUED)
        self._stage(queued)
        self._needs[run.run_id] = dict(job.pools)
        self.ready.push(queued, job.criticality, job.pools)
        return queued

    def _unqueue(self, run_id: str) -> None:
        """Take ``run_id`` out of the ready queue, the force list and its retry wait."""
        self.ready.remove(run_id)
        self._release(run_id)
        self._force.discard(run_id)
        timer = self._retry_timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()

    # -- parts provided by the other engine modules ----------------------

    @abstractmethod
    def _pump(self) -> None:
        """Start the queued runs that fit."""

    @abstractmethod
    def _ready(self, run: Run, job: JobDef) -> list[str]:
        """Queue ``run`` unless the overlap rule holds it back; return newly final jobs."""

    @abstractmethod
    def _reevaluate(self, job_names: Iterable[str]) -> None:
        """Re-check the open runs of ``job_names`` and of their dependents."""

    @abstractmethod
    def _signal_kill(self, run_id: str, handle: AttemptHandle) -> None:
        """Ask the attempt to stop now (SIGTERM), and plan SIGKILL after ``kill_grace``."""

    @abstractmethod
    def _restart_firings(self) -> None:
        """Rebuild the firing heap from now (after a reload)."""
