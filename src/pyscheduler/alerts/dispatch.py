"""Send each alert to its notifiers through PyMQ work queues.

The dispatcher is a post office with one pigeonhole per notifier: every
copy of an alert goes into the pigeonhole of the notifier that must carry
it (the queue ``notify.<name>``). A postman per pigeonhole takes copies
out, tries to deliver them, and either stamps them done (``ack``) or puts
them back to try later (``nack``), waiting longer each time. A copy that
fails too often is dead: it stays in the store as ``dead`` until someone
replays it.

The store is the real record. The broker forgets everything when the
scheduler stops, so on start every ``pending`` delivery is posted again,
and a delivery is written down (``sent``, ``pending`` with its error, or
``dead``) before the broker is told how the try went.
"""

import contextlib
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast

import pynet.loop
from pymq.broker import Broker, LeasedMessage
from pynet.loop import Lock
from pynet.messaging import JsonValue

from pyscheduler.alerts.delivery import (
    ATTEMPTS_HEADER,
    NOT_RUNNING,
    REMOVED_ERROR,
    AlertsStoppedError,
    Pigeonholes,
    max_tries,
    queue_config,
    retry_wait,
)
from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.alerts.routing import alert_topic
from pyscheduler.config.model import NotifierDef
from pyscheduler.config.redaction import redact_text
from pyscheduler.engine.core import Engine
from pyscheduler.notifiers.base import NotifierFactory, NotifyError, build_notifier
from pyscheduler.store.schema import Batch
from pyscheduler.values import format_duration

logger = logging.getLogger(__name__)

SEND_MARGIN_SECONDS = 1.0
"""How much longer than the notifier's timeout one send may take, in real seconds."""

STOP_WAIT_SECONDS = 5.0
"""How long ``stop`` waits for sends already under way."""

SETTLE_NAP_SECONDS = 0.005
"""How long ``settle`` sleeps between looks."""


class Dispatcher(Pigeonholes):
    """Deliver alerts to notifiers through one PyMQ queue per notifier.

    Every broker call happens on the loop thread. A notifier is looked up
    in ``engine.config`` and built afresh for every send, so a reload
    needs no hook; a notifier whose retry settings change gets a fresh
    queue (``notify.<name>.<n>``), because PyMQ will not change a queue's
    settings. A store failure fails the engine stop and ends delivering.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        factory: NotifierFactory = build_notifier,
        time_scale: float = 1.0,
    ) -> None:
        """Prepare a dispatcher; ``time_scale`` multiplies every PyMQ delay and timeout."""
        super().__init__(engine, factory=factory, time_scale=time_scale)
        self._replays = 0
        self._replaying = Lock()
        """Held from reading the dead rows to posting them, so a row is replayed once."""

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Make the broker and post every ``pending`` delivery again, oldest first."""
        self.broker = Broker()
        store = self.engine.store
        waiting = await pynet.loop.run_in_thread(
            store.deliveries_with, DeliveryStatus.PENDING, sys.maxsize
        )
        bodies: dict[str, dict[str, JsonValue] | None] = {}
        for delivery in waiting:
            if delivery.alert_id not in bodies:
                alert = await pynet.loop.run_in_thread(store.get_alert, delivery.alert_id)
                bodies[delivery.alert_id] = None if alert is None else self._body(alert)
            body = bodies[delivery.alert_id]
            if body is not None:
                self._post(delivery, body, delivery.delivery_id)

    async def stop(self) -> None:
        """Stop taking copies, give sends under way up to 5 s, then close the broker."""
        if self._stopped or self.broker is None:
            return
        self._stopped = True
        self._cancel_consumers()
        sending = list(self._sending)
        if sending:
            with contextlib.suppress(TimeoutError):
                await pynet.loop.wait_for(
                    pynet.loop.gather(*sending, return_exceptions=True), STOP_WAIT_SECONDS
                )
            for task in sending:
                task.cancel()
        self.broker.close()

    async def settle(self) -> None:
        """Return once no send is under way and no copy waits (a test helper).

        It also returns once delivering has stopped (a store error, or the
        engine failed).
        """
        while not self._halted() and self.broker is not None and self._busy():
            await pynet.loop.sleep(SETTLE_NAP_SECONDS)

    # -- posting ---------------------------------------------------------

    def enqueue(self, alert: AlertRecord, deliveries: Sequence[DeliveryRecord]) -> None:
        """Publish ``alert`` on its topic and post each delivery (the alert is durable).

        Raises:
            RuntimeError: If the dispatcher was not started.

        """
        broker = self._require()
        if self.error is not None or self._stopped:
            return  # the deliveries stay pending in the store for the next start
        body = self._body(alert)
        broker.publish(alert_topic(alert), body)
        for delivery in deliveries:
            self._post(delivery, body, delivery.delivery_id)

    async def replay(self, alert_id: str, notifier: str | None = None) -> list[DeliveryRecord]:
        """Post an alert's dead deliveries (or just ``notifier``'s) again, from zero tries.

        Replays take turns, so two at once never post the same row twice:
        the second finds it ``pending`` and raises ``LookupError``.

        A dead delivery whose notifier has since been removed stays dead.

        Raises:
            LookupError: If the alert has no such dead delivery (or only
                ones whose notifier was removed).
            AlertsStoppedError: If the dispatcher has stopped, or the
                engine has failed.
            RuntimeError: If the dispatcher was not started.
            Exception: The store's error, if saving the rows failed (the
                engine then fails stop, as for any lost write).

        """
        self._require()
        async with self._replaying:
            if self._halted():
                raise AlertsStoppedError(NOT_RUNNING)
            return await self._replay(alert_id, notifier)

    async def _replay(self, alert_id: str, notifier: str | None) -> list[DeliveryRecord]:
        """Do the work of ``replay`` (the replay lock is held)."""
        store = self.engine.store
        found = await pynet.loop.run_in_thread(store.deliveries_for, [alert_id])
        dead = [
            d
            for d in found.get(alert_id, [])
            if d.status is DeliveryStatus.DEAD and notifier in (None, d.notifier)
        ]
        alert = await pynet.loop.run_in_thread(store.get_alert, alert_id) if dead else None
        if alert is None:
            message = f"no dead delivery for {alert_id}"
            raise LookupError(message)
        removed = sorted(d.notifier for d in dead if d.notifier not in self.engine.config.notifiers)
        dead = [d for d in dead if d.notifier not in removed]
        if not dead:
            message = f"cannot replay {alert_id}: notifier removed ({', '.join(removed)})"
            raise LookupError(message)
        now = self.engine.clock.now()
        fresh = [
            replace(d, status=DeliveryStatus.PENDING, attempts=0, last_error="", updated_at=now)
            for d in dead
        ]
        if not await self._save(Batch(deliveries=fresh)):
            raise self.error or AlertsStoppedError(NOT_RUNNING)
        body = self._body(alert)
        for delivery in fresh:
            self._replays += 1  # a fresh message id: PyMQ remembers ids it has seen
            self._post(delivery, body, f"{delivery.delivery_id}#{self._replays}")
        return fresh

    # -- delivering ------------------------------------------------------

    def _sink(self, message: LeasedMessage) -> None:
        """Start delivering a leased copy (PyMQ calls this; it must not block)."""
        self._track(self._deliver(message))

    async def _deliver(self, message: LeasedMessage) -> None:
        """Send one copy, write down how it went, and settle it, all under a kept lease.

        The lease is renewed from the start of the send until the copy is
        settled, so neither a slow send nor a slow store write lets PyMQ
        hand the copy out a second time.
        """
        keeper = pynet.loop.spawn(self._keep_leased(message), name="alert-lease")
        try:
            await self._try_once(message)
        finally:
            keeper.cancel()

    async def _try_once(self, message: LeasedMessage) -> None:
        """Try one send, write the row, then ack or nack the copy."""
        if self.engine.error is not None:  # the engine failed stop: deliver nothing more
            self._cancel_consumers()
            return
        envelope = message.envelope
        alert_id = envelope.headers["alert_id"]
        name = envelope.headers["notifier"]
        attempts = int(envelope.headers.get(ATTEMPTS_HEADER, "0")) + envelope.attempt
        definition = self.engine.config.notifiers.get(name)
        if definition is None:
            record = self._record(alert_id, name, DeliveryStatus.DEAD, attempts - 1, REMOVED_ERROR)
            if await self._write(record):
                self._settle(message, ack=True)
            return
        error = await self._send(definition, envelope.body)
        if error is None:
            if await self._write(self._record(alert_id, name, DeliveryStatus.SENT, attempts, "")):
                self._settle(message, ack=True)
            return
        error = redact_text(error, self.engine.config.expansions)
        dead = attempts >= max_tries(definition)  # the current retries, not the queue's
        giving_up = ", giving up" if dead else ""
        logger.warning(
            "alert %s to %s failed (try %d%s): %s", alert_id, name, attempts, giving_up, error
        )
        status = DeliveryStatus.DEAD if dead else DeliveryStatus.PENDING
        record = self._record(alert_id, name, status, attempts, error)
        if not await self._write(record):
            return
        if not dead and attempts >= self._queues[message.queue].max_attempts:
            # An old queue (retries raised by a reload) would bury it: move it on.
            self._settle(message, ack=True)
            self._replays += 1
            delay = retry_wait(definition, attempts, self._scale)
            body = cast("dict[str, JsonValue]", envelope.body)
            self._post(record, body, f"{record.delivery_id}#{self._replays}", delay=delay)
            return
        self._settle(message, ack=False, requeue=not dead, error=error)

    async def _send(self, definition: NotifierDef, body: JsonValue) -> str | None:
        """Send once within the notifier's real timeout; return the error text, or ``None``.

        Every ``Exception`` is a failed try. Only the send itself is timed.
        """
        limit = pynet.loop.timeout(definition.timeout.total_seconds() + SEND_MARGIN_SECONDS)
        try:
            notifier = self._factory(definition, self.engine.config.settings)
            async with limit:
                await notifier.send(cast("Mapping[str, JsonValue]", body))
        except Exception as exc:  # noqa: BLE001 -- any failure is a failed try
            if isinstance(exc, TimeoutError) and limit.expired():
                return f"send timed out after {format_duration(definition.timeout)}"
            return str(exc) if isinstance(exc, NotifyError) else f"{type(exc).__name__}: {exc}"
        return None


__all__ = ["Dispatcher", "queue_config"]
