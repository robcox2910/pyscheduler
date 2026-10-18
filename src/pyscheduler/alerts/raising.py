"""Raise one alert: word it, write it down, then hand it to the dispatcher.

Raising is the same for every kind, like posting a letter: check the
job wants this kind, check we have not posted the very same letter
already (the same id), write the alert and one ``pending`` delivery per
notifier to the store, and only then give it to the post office. The
``AlertMonitor`` builds on this with the part that decides *when* to raise.
"""

import logging
from abc import ABC, abstractmethod

import pynet.loop
from pynet.loop import Task

from pyscheduler.alerts.deadlines import Wording
from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus, alert_id_for
from pyscheduler.alerts.routing import destinations
from pyscheduler.config.model import AlertKind, Criticality, JobAlerts, JobDef
from pyscheduler.config.redaction import redact_text
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import parse_run_id
from pyscheduler.store.schema import Batch

logger = logging.getLogger("pyscheduler.alerts.monitor")


class AlertRaiser(ABC):
    """Write alerts (once per id) and enqueue them; the base of ``AlertMonitor``."""

    def __init__(self, engine: Engine, dispatcher: Dispatcher) -> None:
        """Remember the engine and the dispatcher; nothing has been raised yet."""
        self.engine = engine
        self.dispatcher = dispatcher
        self._seen: set[str] = set()
        """Ids of the alerts already raised: a second one with the same id is skipped."""
        self._stopped = False
        self._broken = False
        """Set once an alert could not be written: the engine has failed stop."""
        self._tasks: set[Task[None]] = set()

    @abstractmethod
    def _cancel_timers(self) -> None:
        """Call off every timer (the engine has failed stop)."""

    async def settle(self) -> None:
        """Wait until no alert is being raised (a test helper)."""
        while self._tasks:
            await pynet.loop.gather(*list(self._tasks), return_exceptions=True)

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
        """Store the alert with a ``pending`` delivery per notifier, then enqueue it.

        An alert already in the store (raised before the monitor forgot
        its id) is left alone: its deliveries, perhaps long sent, are
        never reset to ``pending``.
        """
        try:
            stored = await pynet.loop.run_in_thread(self.engine.store.get_alert, alert.alert_id)
        except Exception as exc:  # noqa: BLE001 -- the store failed: fail the engine stop
            self._fail(exc)
            return
        if stored is not None:
            return
        try:
            names = destinations(self.engine.config, alert)
        except Exception:
            logger.exception("could not route alert %s", alert.alert_id)
            return
        deliveries = [
            DeliveryRecord(alert.alert_id, name, DeliveryStatus.PENDING, 0, "", alert.raised_at)
            for name in names
        ]
        try:
            await self.engine.writer.save(Batch(alerts=[alert], deliveries=deliveries))
        except Exception as exc:  # noqa: BLE001 -- a lost write fails the engine stop
            self._fail(exc)
            return
        try:
            self.dispatcher.enqueue(alert, deliveries)
        except Exception:
            logger.exception("could not hand alert %s to the dispatcher", alert.alert_id)

    def _fail(self, exc: BaseException) -> None:
        """Stop raising and fail the engine stop: the store could not be used."""
        self._broken = True
        self._cancel_timers()
        self.engine.fail_stop(exc)

    def _job_of(self, run_id: str) -> JobDef | None:
        """Return the configured job of ``run_id``, or ``None``."""
        run = self.engine.book.get(run_id)
        name = run.job if run is not None else parse_run_id(run_id)[0]
        return self.engine.config.jobs.get(name)

    def _quiet(self) -> bool:
        """Return whether the monitor raises nothing more (stopped, or the engine failed)."""
        return self._stopped or self._broken or self.engine.error is not None


__all__ = ["AlertRaiser"]
