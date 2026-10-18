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
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

import pynet.loop
from pymq.broker import Broker, BrokerError, LeasedMessage, QueueConfig
from pynet.loop import Task
from pynet.messaging import JsonValue

from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.alerts.routing import alert_json, alert_topic
from pyscheduler.config.model import NotifierDef
from pyscheduler.config.redaction import redact_text
from pyscheduler.engine.core import Engine
from pyscheduler.notifiers.base import NotifierFactory, NotifyError, build_notifier
from pyscheduler.store.schema import Batch
from pyscheduler.values import format_duration

logger = logging.getLogger(__name__)

ACK_GRACE_SECONDS = 5.0
"""How much longer than the notifier's timeout a lease lasts."""

SEND_MARGIN_SECONDS = 1.0
"""How much longer than the notifier's timeout one send may take, in real seconds."""

PREFETCH = 4
"""How many copies one notifier may be sending at once."""

STOP_WAIT_SECONDS = 5.0
"""How long ``stop`` waits for sends already under way."""

SETTLE_NAP_SECONDS = 0.005
"""How long ``settle`` sleeps between looks."""

ATTEMPTS_HEADER = "attempts"
"""The header carrying the tries a delivery had before this message was posted."""

REMOVED_ERROR = "notifier removed"
"""The error of a delivery whose notifier is no longer configured."""


def queue_config(definition: NotifierDef, time_scale: float = 1.0) -> QueueConfig:
    """Return the queue settings for a notifier: its lease, tries and back-off."""
    retries = definition.retries
    delay = retries.delay.total_seconds() * time_scale
    return QueueConfig(
        ack_timeout=(definition.timeout.total_seconds() + ACK_GRACE_SECONDS) * time_scale,
        max_attempts=retries.count + 1,
        retry_delay=delay,
        max_retry_delay=max(retries.max_delay.total_seconds() * time_scale, delay),
    )


class Dispatcher:
    """Deliver alerts to notifiers through one PyMQ queue per notifier.

    Every broker call happens on the loop thread. A notifier is looked up
    in ``engine.config`` and built afresh for every send, so a reload
    needs no hook; a notifier whose retry settings change gets a fresh
    queue (``notify.<name>.<n>``), because PyMQ will not change a queue's
    settings.

    Attributes:
        engine: The engine whose config, store, writer and clock are used.
        broker: The in-process broker, made by ``start``.
        error: The store error that stopped delivering, or ``None``.

    """

    def __init__(
        self,
        engine: Engine,
        *,
        factory: NotifierFactory = build_notifier,
        time_scale: float = 1.0,
    ) -> None:
        """Prepare a dispatcher; ``time_scale`` multiplies every PyMQ delay and timeout."""
        self.engine = engine
        self.broker: Broker | None = None
        self.error: BaseException | None = None
        self._factory = factory
        self._scale = time_scale
        self._lanes: dict[str, tuple[str, QueueConfig]] = {}
        """Each notifier's current queue and its settings."""
        self._queues: dict[str, QueueConfig] = {}
        """Every queue declared so far, current or not, with its settings."""
        self._generations: dict[str, int] = {}
        self._consumers: list[str] = []
        self._sending: set[Task[None]] = set()
        self._replays = 0
        self._stopped = False

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

        It also returns once the dispatcher has stopped on a store error.
        """
        while self.error is None and self.broker is not None and self._busy():
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

        Raises:
            LookupError: If the alert has no such dead delivery.
            RuntimeError: If the dispatcher was not started.

        """
        self._require()
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
        now = self.engine.clock.now()
        fresh = [
            replace(d, status=DeliveryStatus.PENDING, attempts=0, last_error="", updated_at=now)
            for d in dead
        ]
        await self.engine.writer.save(Batch(deliveries=fresh))
        body = self._body(alert)
        for delivery in fresh:
            self._replays += 1  # a fresh message id: PyMQ remembers ids it has seen
            self._post(delivery, body, f"{delivery.delivery_id}#{self._replays}")
        return fresh

    def _post(self, delivery: DeliveryRecord, body: dict[str, JsonValue], message_id: str) -> None:
        """Put one delivery on its notifier's queue (or bury it if the notifier is gone)."""
        if self.error is not None:
            return
        queue = self._queue_for(delivery.notifier)
        if queue is None:
            self._track(self._bury(delivery))
            return
        headers = {
            "alert_id": delivery.alert_id,
            "notifier": delivery.notifier,
            ATTEMPTS_HEADER: str(delivery.attempts),
        }
        try:
            self._require().put(queue, body, id=message_id, headers=headers)
        except BrokerError as exc:  # it stays pending in the store; the next start posts it
            logger.error("cannot queue alert %s for %s: %s", delivery.alert_id, queue, exc)  # noqa: TRY400

    def _queue_for(self, name: str) -> str | None:
        """Return the notifier's current queue, declaring a fresh one when its settings changed."""
        definition = self.engine.config.notifiers.get(name)
        if definition is None:
            return None
        config = queue_config(definition, self._scale)
        lane = self._lanes.get(name)
        if lane is not None and lane[1] == config:
            return lane[0]
        generation = self._generations.get(name, 0)
        self._generations[name] = generation + 1
        queue = f"notify.{name}" if generation == 0 else f"notify.{name}.{generation}"
        broker = self._require()
        broker.declare_queue(queue, config)
        self._consumers.append(broker.consume(queue, self._sink, prefetch=PREFETCH))
        self._lanes[name] = (queue, config)
        self._queues[queue] = config
        return queue

    # -- delivering ------------------------------------------------------

    def _sink(self, message: LeasedMessage) -> None:
        """Start delivering a leased copy (PyMQ calls this; it must not block)."""
        self._track(self._deliver(message))

    async def _deliver(self, message: LeasedMessage) -> None:
        """Try one send, write down how it went, then settle the copy with the broker."""
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
        error = await self._send(definition, envelope.body, message)
        if error is None:
            if await self._write(self._record(alert_id, name, DeliveryStatus.SENT, attempts, "")):
                self._settle(message, ack=True)
            return
        error = redact_text(error, self.engine.config.expansions)
        dead = attempts >= self._queues[message.queue].max_attempts
        giving_up = ", giving up" if dead else ""
        logger.warning(
            "alert %s to %s failed (try %d%s): %s", alert_id, name, attempts, giving_up, error
        )
        status = DeliveryStatus.DEAD if dead else DeliveryStatus.PENDING
        if await self._write(self._record(alert_id, name, status, attempts, error)):
            self._settle(message, ack=False, requeue=not dead, error=error)

    async def _send(
        self, definition: NotifierDef, body: JsonValue, message: LeasedMessage
    ) -> str | None:
        """Send once within the notifier's real timeout; return the error text, or ``None``.

        Every ``Exception`` is a failed try. The lease is renewed while the
        send runs, so a slow send is never handed out a second time.
        """
        keeper = pynet.loop.spawn(self._keep_leased(message), name="alert-lease")
        limit = pynet.loop.timeout(definition.timeout.total_seconds() + SEND_MARGIN_SECONDS)
        try:
            notifier = self._factory(definition, self.engine.config.settings)
            async with limit:
                await notifier.send(cast("Mapping[str, JsonValue]", body))
        except Exception as exc:  # noqa: BLE001 -- any failure is a failed try
            if isinstance(exc, TimeoutError) and limit.expired():
                return f"send timed out after {format_duration(definition.timeout)}"
            return str(exc) if isinstance(exc, NotifyError) else f"{type(exc).__name__}: {exc}"
        finally:
            keeper.cancel()
        return None

    async def _keep_leased(self, message: LeasedMessage) -> None:
        """Renew the copy's lease every half lease until cancelled."""
        every = self._queues[message.queue].ack_timeout / 2
        while True:
            await pynet.loop.sleep(every)
            with contextlib.suppress(BrokerError):
                self._require().touch(
                    message.queue, message.delivery_tag, consumer_tag=message.consumer_tag
                )

    async def _bury(self, delivery: DeliveryRecord) -> None:
        """Write a delivery whose notifier is gone as ``dead``."""
        alert_id, name, dead = delivery.alert_id, delivery.notifier, DeliveryStatus.DEAD
        await self._write(self._record(alert_id, name, dead, delivery.attempts, REMOVED_ERROR))

    def _settle(
        self,
        message: LeasedMessage,
        *,
        ack: bool,
        requeue: bool = True,
        error: str | None = None,
    ) -> None:
        """Ack or nack a copy; a broker that has closed meanwhile is ignored."""
        broker = self._require()
        with contextlib.suppress(BrokerError):
            if ack:
                broker.ack(message.queue, message.delivery_tag, consumer_tag=message.consumer_tag)
            else:
                broker.nack(
                    message.queue,
                    message.delivery_tag,
                    requeue=requeue,
                    error=error,
                    consumer_tag=message.consumer_tag,
                )

    # -- writing ---------------------------------------------------------

    def _record(
        self, alert_id: str, name: str, status: DeliveryStatus, attempts: int, error: str
    ) -> DeliveryRecord:
        """Return a delivery row stamped with the engine's time."""
        return DeliveryRecord(alert_id, name, status, attempts, error, self.engine.clock.now())

    async def _write(self, record: DeliveryRecord) -> bool:
        """Save one delivery row; on a store error stop delivering and return ``False``."""
        if self.error is not None:
            return False
        try:
            await self.engine.writer.save(Batch(deliveries=[record]))
        except Exception as exc:  # noqa: BLE001 -- the engine fails stop; we stop too
            if self.error is None:
                self.error = exc
                logger.error("alert delivery stopped: the store failed: %r", exc)  # noqa: TRY400
                self._cancel_consumers()
            return False
        return True

    # -- helpers ---------------------------------------------------------

    def _body(self, alert: AlertRecord) -> dict[str, JsonValue]:
        """Return the JSON a notifier is sent for ``alert``."""
        return alert_json(alert, scheduler=self.engine.config.settings.name)

    def _require(self) -> Broker:
        """Return the broker.

        Raises:
            RuntimeError: If ``start`` has not made it yet.

        """
        if self.broker is None:
            message = "the dispatcher is not started"
            raise RuntimeError(message)
        return self.broker

    def _cancel_consumers(self) -> None:
        """Stop every postman taking copies."""
        broker = self._require()
        for tag in self._consumers:
            with contextlib.suppress(BrokerError):
                broker.cancel(tag)
        self._consumers.clear()

    def _track(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run ``coro`` as a task that ``settle`` and ``stop`` wait for."""
        task = pynet.loop.spawn(coro, name="alert-delivery")
        self._sending.add(task)
        task.add_done_callback(lambda _: self._sending.discard(task))

    def _busy(self) -> bool:
        """Return whether a send is under way or a copy is ready, waiting or leased."""
        if self._sending:
            return True
        broker = self._require()
        for queue in self._queues:
            stats = broker.stats(queue)
            if stats.ready or stats.delayed or stats.in_flight:
                return True
        return False


__all__ = ["Dispatcher", "queue_config"]
