"""Watch the engine and raise an alert when something needs a human.

The monitor is a smoke alarm that also takes the register. Some alerts
come the moment something happens: a run failed, is blocked, is retrying,
an attempt was lost or ended suspiciously quickly, or a file watcher
gave up waiting. Others come from
noticing that something did *not* happen: an attempt still running past
its ``max_runtime``, or a run that has not started by its
``must_start_by``. Those are timers, set when the run or attempt is first
seen and called off when it starts or ends.

Raising an alert is always the same: check the job wants this kind, skip
an alert already raised (the same id), write the alert and one
``pending`` delivery per notifier to the store, and only then hand it to
the dispatcher. A failed alert never stops jobs: a timer's error is
logged, not fail-stopped. The one exception is the store: if the alert
cannot be written, the engine fails stop, as it would for any lost write.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta

import pynet.loop
from pynet.loop import Task

from pyscheduler.alerts import deadlines
from pyscheduler.alerts.deadlines import Wording
from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import (
    AlertRecord,
    DeliveryRecord,
    DeliveryStatus,
    alert_id_for,
)
from pyscheduler.alerts.routing import destinations
from pyscheduler.config.model import AlertKind, Criticality, JobAlerts, JobDef, JobKind, Problem
from pyscheduler.config.redaction import redact_text
from pyscheduler.engine.clock import Cancel
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Outcome, Run, RunState, parse_run_id
from pyscheduler.store.schema import Batch

logger = logging.getLogger(__name__)

NOT_STARTED = frozenset(
    {RunState.SCHEDULED, RunState.WAITING, RunState.BLOCKED, RunState.QUEUED, RunState.HELD}
)
"""The states of a run that is still waiting for its first attempt."""

PRUNE_EVERY = timedelta(hours=1)
"""How often the remembered alert ids are trimmed to the runs still in memory."""


class AlertMonitor:
    """Raise alerts from the engine's run and attempt changes, and from deadlines.

    Build it *before* ``engine.start()``, so it hears what recovery does;
    it holds on to what it hears until ``start`` (after the dispatcher's
    ``start``).
    """

    def __init__(self, engine: Engine, dispatcher: Dispatcher) -> None:
        """Listen to ``engine``'s runs and attempts; nothing is raised until ``start``."""
        self.engine = engine
        self.dispatcher = dispatcher
        self._seen: set[str] = set()
        """Ids of the alerts already raised: a second one with the same id is skipped."""
        self._early: list[Callable[[], None]] = []
        self._started = False
        self._stopped = False
        self._broken = False
        """Set once an alert could not be written: the engine has failed stop."""
        self._late_timers: dict[str, tuple[datetime, Cancel]] = {}
        """Each waiting run's ``never_started`` timer, with the deadline it rings at."""
        self._slow_timers: dict[tuple[str, int], tuple[datetime, Attempt, Cancel]] = {}
        """Each running attempt's ``too_long`` timer, with when it rings and the attempt."""
        self._prune_timer: Cancel | None = None
        self._tasks: set[Task[None]] = set()
        self._unhook = [
            engine.subscribe(self._on_run),
            engine.watch_attempts(self._on_attempt),
            engine.watch_reloads(self._on_reload),
            engine.before_close(self.stop),  # keep listening while the engine winds down
        ]

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Remember recent alert ids, catch up on what was heard, and arm the deadlines.

        The ids are those raised since midnight UTC of the engine's memory
        start, plus every alert of an open run (an open run can be older
        than that window). Deadlines are armed for the open runs and
        running attempts already in memory. A start deadline already past
        rings at once only if it passed while the scheduler was down
        (after the engine's ``last_active``).
        """
        if self._stopped:
            return
        midnight = datetime.combine(self.engine.memory_start(), time(), tzinfo=UTC)
        store = self.engine.store
        self._seen |= await pynet.loop.run_in_thread(store.alert_ids_since, midnight)
        open_ids = [run.run_id for run in self.engine.book.open_runs()]
        self._seen |= await pynet.loop.run_in_thread(store.alert_ids_of_runs, open_ids)
        self._started = True
        early, self._early = self._early, []
        for heard in early:
            heard()
        runs = self.engine.book.open_runs()
        for run in runs:
            self._watch_start(run, restarting=True)
        running = [run.run_id for run in runs if run.state is RunState.RUNNING]
        latest = await pynet.loop.run_in_thread(store.latest_attempts, running)
        for attempt in latest.values():
            if attempt.ended_at is None:
                self._watch_runtime(attempt)  # skipped if it ended while we read
        self._plan_prune()

    async def stop(self) -> None:
        """Stop listening, call off every timer, and wait for alerts being raised.

        ``Engine.stop`` calls it just before its writer closes, so runs
        that end while the engine winds down still get their alerts. It
        is safe before ``start`` (what was heard early is dropped) and twice.
        """
        self._stopped = True
        self._early.clear()
        for unhook in self._unhook:
            unhook()
        self._cancel_timers()
        await self.settle()

    async def settle(self) -> None:
        """Wait until no alert is being raised (a test helper)."""
        while self._tasks:
            await pynet.loop.gather(*list(self._tasks), return_exceptions=True)

    # -- scheduler-wide alerts -------------------------------------------

    def reload_failed(self, problems: Sequence[Problem]) -> None:
        """Raise a ``reload_failed`` alert (criticality high, no run) for ``problems``."""
        if self._stopped:
            return
        if not self._started:
            self._early.append(lambda: self.reload_failed(problems))
            return
        now = self.engine.clock.now()
        alert = deadlines.reload_failed(now, problems, self.engine.config.expansions)
        self._raise(alert)

    # -- what the engine tells us ----------------------------------------

    def _on_run(self, run: Run) -> None:
        """Raise the alerts a run's new state calls for, and watch its start deadline."""
        if self._stopped:
            return
        if not self._started:
            self._early.append(lambda: self._on_run(run))
            return
        if run.state is RunState.FAILED:
            self._raise_for(run.run_id, AlertKind.FAILED, run.attempt, deadlines.failed(run))
        elif run.state is RunState.RETRY_WAIT:
            self._raise_for(run.run_id, AlertKind.RETRYING, run.attempt, deadlines.retrying(run))
        elif run.state is RunState.BLOCKED:
            self._raise_for(run.run_id, AlertKind.BLOCKED, run.attempt, deadlines.blocked(run))
        self._watch_start(run)

    def _on_attempt(self, attempt: Attempt) -> None:
        """Time a started attempt; check how an ended one went."""
        if self._stopped:
            return
        if not self._started:
            self._early.append(lambda: self._on_attempt(attempt))
            return
        if attempt.ended_at is None:
            self._watch_runtime(attempt)
            return
        armed = self._slow_timers.pop((attempt.run_id, attempt.attempt), None)
        if armed is not None:
            armed[2].cancel()
        run_id, number = attempt.run_id, attempt.attempt
        job = self._job_of(run_id)
        if attempt.outcome is Outcome.LOST:
            self._raise_for(run_id, AlertKind.LOST, number, deadlines.lost(attempt))
        elif attempt.outcome is Outcome.TIMEOUT and job and job.kind is JobKind.FILE_WATCHER:
            self._raise_for(run_id, AlertKind.WATCHER_TIMEOUT, number, deadlines.gave_up(attempt))
        if attempt.outcome is not Outcome.SUCCEEDED or job is None or job.min_runtime is None:
            return
        runtime = attempt.ended_at - attempt.started_at
        if runtime < job.min_runtime:
            wording = deadlines.too_short(attempt, runtime, job.min_runtime)
            self._raise_for(run_id, AlertKind.TOO_SHORT, number, wording)

    # -- deadlines -------------------------------------------------------

    def _on_reload(self) -> None:
        """Set every timer again from the new config (a deadline may move, or go)."""
        if self._stopped or not self._started:
            return
        for run_id in list(self._late_timers):
            run = self.engine.book.get(run_id)
            if run is None:
                self._late_timers.pop(run_id)[1].cancel()
            else:
                self._watch_start(run)
        for _, attempt, _ in list(self._slow_timers.values()):
            self._watch_runtime(attempt)

    def _watch_start(self, run: Run, *, restarting: bool = False) -> None:
        """Keep ``run``'s ``must_start_by`` timer in step with its state and the config.

        The timer is armed while the run waits for its first attempt, set
        again if the deadline moved, and called off once there is none.
        A run heard live is newly armed only for a deadline still ahead
        (a rerun, or a reload adding ``must_start_by``, never rings for a
        deadline long gone). At a restart a past deadline rings at once
        only if it fell while the scheduler was down.
        """
        deadline = self._start_deadline(run)
        armed = self._late_timers.get(run.run_id)
        if armed is not None and armed[0] == deadline:
            return
        if armed is not None:
            del self._late_timers[run.run_id]
            armed[1].cancel()
        alert_id = alert_id_for(run.run_id, AlertKind.NEVER_STARTED, 0)
        if deadline is None or self._quiet() or alert_id in self._seen:
            return
        if deadline <= self.engine.clock.now() and armed is None:  # a moved timer still rings
            last = self.engine.last_active
            if not restarting or last is None or deadline <= last:
                return
        rings = self._safe(lambda: self._not_started(run.run_id))
        self._late_timers[run.run_id] = (deadline, self.engine.clock.call_at(deadline, rings))

    def _start_deadline(self, run: Run) -> datetime | None:
        """Return ``run``'s start deadline under the current config, or ``None`` for none."""
        if run.state not in NOT_STARTED or run.attempt != 0:
            return None
        job = self.engine.config.jobs.get(run.job)
        return None if job is None else deadlines.start_deadline(run, job)

    def _not_started(self, run_id: str) -> None:
        """Raise ``never_started`` if the run still waits and its deadline (now) has passed."""
        self._late_timers.pop(run_id, None)
        run = self.engine.book.get(run_id)
        job = None if run is None else self.engine.config.jobs.get(run.job)
        deadline = None if run is None else self._start_deadline(run)
        if run is None or job is None or deadline is None:
            return
        if deadline > self.engine.clock.now():  # moved later by a reload
            self._watch_start(run)
            return
        wording = deadlines.never_started(run, deadline, job)
        self._raise_for(run_id, AlertKind.NEVER_STARTED, 0, wording)

    def _watch_runtime(self, attempt: Attempt) -> None:
        """Keep a running attempt's ``max_runtime`` timer in step with the config."""
        key = (attempt.run_id, attempt.attempt)
        due = self._runtime_due(attempt)
        armed = self._slow_timers.get(key)
        if armed is not None and armed[0] == due:
            return
        if armed is not None:
            del self._slow_timers[key]
            armed[2].cancel()
        if due is None or self._quiet():
            return
        rings = self._safe(lambda: self._overran(attempt))
        self._slow_timers[key] = (due, attempt, self.engine.clock.call_at(due, rings))

    def _runtime_due(self, attempt: Attempt) -> datetime | None:
        """Return when ``attempt`` runs too long, or ``None`` (not running, or no limit)."""
        run = self.engine.book.get(attempt.run_id)
        if run is None or run.state is not RunState.RUNNING or run.attempt != attempt.attempt:
            return None
        job = self.engine.config.jobs.get(run.job)
        if job is None or job.max_runtime is None:
            return None
        return attempt.started_at + job.max_runtime

    def _overran(self, attempt: Attempt) -> None:
        """Raise ``too_long`` if the attempt still runs past its (current) ``max_runtime``."""
        self._slow_timers.pop((attempt.run_id, attempt.attempt), None)
        due = self._runtime_due(attempt)
        now = self.engine.clock.now()
        if due is None:
            return
        if due > now:  # moved later by a reload
            self._watch_runtime(attempt)
            return
        runtime, limit = now - attempt.started_at, due - attempt.started_at
        wording = deadlines.too_long(attempt, runtime, limit)
        self._raise_for(attempt.run_id, AlertKind.TOO_LONG, attempt.attempt, wording)

    def _plan_prune(self) -> None:
        """Plan the next trim of the remembered alert ids."""
        if self._quiet():
            return
        when = self.engine.clock.now() + PRUNE_EVERY
        self._prune_timer = self.engine.clock.call_at(when, self._safe(self._prune))

    def _prune(self) -> None:
        """Forget the ids of alerts whose run has left memory (and of run-less ones)."""
        book = self.engine.book
        self._seen = {i for i in self._seen if book.get(i.split(":", 1)[0]) is not None}
        self._plan_prune()

    # -- raising ---------------------------------------------------------

    def _raise_for(self, run_id: str, kind: AlertKind, attempt: int, wording: Wording) -> None:
        """Raise a ``kind`` alert about ``run_id`` if its job wants that kind."""
        run = self.engine.book.get(run_id)
        name, run_date, _ = parse_run_id(run_id) if run is None else (run.job, run.run_date, 0)
        job = self.engine.config.jobs.get(name)
        if not (job.alerts if job is not None else JobAlerts()).enabled(kind):
            return
        message, detail = wording
        expansions = self.engine.config.expansions
        alert = AlertRecord(
            alert_id=alert_id_for(run_id, kind, attempt),
            kind=kind,
            run_id=run_id,
            job=name,
            run_date=run_date,
            attempt=attempt,
            criticality=job.criticality if job is not None else Criticality.NORMAL,
            raised_at=self.engine.clock.now(),
            message=redact_text(message, expansions),
            detail={
                k: redact_text(v, expansions) if isinstance(v, str) else v
                for k, v in detail.items()
            },
        )
        self._raise(alert)

    def _raise(self, alert: AlertRecord) -> None:
        """Write ``alert`` and its deliveries, then hand it to the dispatcher (in a task)."""
        if self._quiet() or alert.alert_id in self._seen:
            return
        self._seen.add(alert.alert_id)
        task = pynet.loop.spawn(self._commit(alert), name=f"alert-{alert.alert_id}")
        self._tasks.add(task)
        task.add_done_callback(lambda _: self._tasks.discard(task))

    async def _commit(self, alert: AlertRecord) -> None:
        """Store the alert with a ``pending`` delivery per notifier, then enqueue it."""
        names = destinations(self.engine.config, alert)
        deliveries = [
            DeliveryRecord(alert.alert_id, name, DeliveryStatus.PENDING, 0, "", alert.raised_at)
            for name in names
        ]
        try:
            await self.engine.writer.save(Batch(alerts=[alert], deliveries=deliveries))
        except Exception as exc:  # noqa: BLE001 -- a lost write fails the engine stop
            self._broken = True
            self._cancel_timers()
            self.engine.fail_stop(exc)
            return
        try:
            self.dispatcher.enqueue(alert, deliveries)
        except Exception:
            logger.exception("could not hand alert %s to the dispatcher", alert.alert_id)

    # -- small helpers ---------------------------------------------------

    def _job_of(self, run_id: str) -> JobDef | None:
        """Return the configured job of ``run_id``, or ``None``."""
        run = self.engine.book.get(run_id)
        name = run.job if run is not None else parse_run_id(run_id)[0]
        return self.engine.config.jobs.get(name)

    def _quiet(self) -> bool:
        """Return whether the monitor raises nothing more (stopped, or the engine failed)."""
        return self._stopped or self._broken or self.engine.error is not None

    def _safe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Wrap a timer callback so an error in it is logged, never fail-stopped."""

        def safe() -> None:
            """Run the callback, logging any error."""
            try:
                callback()
            except Exception:
                logger.exception("alert monitor timer failed")

        return safe

    def _cancel_timers(self) -> None:
        """Call off every deadline and the prune timer."""
        for _, timer in self._late_timers.values():
            timer.cancel()
        for _, _, timer in self._slow_timers.values():
            timer.cancel()
        self._late_timers.clear()
        self._slow_timers.clear()
        if self._prune_timer is not None:
            self._prune_timer.cancel()
            self._prune_timer = None


__all__ = ["NOT_STARTED", "PRUNE_EVERY", "AlertMonitor"]
