"""The pigeonholes of the alert post office: one PyMQ queue per notifier.

``Pigeonholes`` holds the broker and the queues, posts copies into them,
and writes delivery rows to the store. The ``Dispatcher`` builds on it
with the postman: taking a copy out, sending it, and settling it.
"""

import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any

import pynet.loop
from pymq.broker import Broker, BrokerError, LeasedMessage, QueueConfig
from pynet.loop import Task
from pynet.messaging import JsonValue

from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.alerts.routing import alert_json
from pyscheduler.config.model import NotifierDef
from pyscheduler.engine.core import Engine
from pyscheduler.notifiers.base import NotifierFactory
from pyscheduler.store.schema import Batch

logger = logging.getLogger(__name__)

ACK_GRACE_SECONDS = 5.0
"""How much longer than the notifier's timeout a lease lasts."""

PREFETCH = 4
"""How many copies one notifier may be sending at once."""

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


class Pigeonholes(ABC):
    """Hold the broker and one queue per notifier; post copies and write rows.

    Attributes:
        engine: The engine whose config, store, writer and clock are used.
        broker: The in-process broker, made by ``start``.
        error: The store error that stopped delivering, or ``None``.

    """

    def __init__(self, engine: Engine, *, factory: NotifierFactory, time_scale: float) -> None:
        """Prepare empty pigeonholes; ``time_scale`` multiplies every PyMQ delay and timeout."""
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
        self._stopped = False

    @abstractmethod
    def _sink(self, message: LeasedMessage) -> None:
        """Take a leased copy from a queue (PyMQ calls this; it must not block)."""

    # -- posting ---------------------------------------------------------

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

    async def _bury(self, delivery: DeliveryRecord) -> None:
        """Write a delivery whose notifier is gone as ``dead``."""
        alert_id, name, dead = delivery.alert_id, delivery.notifier, DeliveryStatus.DEAD
        await self._write(self._record(alert_id, name, dead, delivery.attempts, REMOVED_ERROR))

    # -- the broker side of one copy -------------------------------------

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
        tag, consumer = message.delivery_tag, message.consumer_tag
        with contextlib.suppress(BrokerError):
            if ack:
                broker.ack(message.queue, tag, consumer_tag=consumer)
            else:
                broker.nack(message.queue, tag, requeue=requeue, error=error, consumer_tag=consumer)

    async def _keep_leased(self, message: LeasedMessage) -> None:
        """Renew the copy's lease every half lease until cancelled, or until it is lost."""
        every = self._queues[message.queue].ack_timeout / 2
        while True:
            await pynet.loop.sleep(every)
            try:
                kept = self._require().touch(
                    message.queue, message.delivery_tag, consumer_tag=message.consumer_tag
                )
            except BrokerError:
                kept = False
            if not kept:
                logger.warning(
                    "lost the lease on alert %s for %s; it may be sent again",
                    message.envelope.headers.get("alert_id"),
                    message.queue,
                )
                return

    # -- writing ---------------------------------------------------------

    def _record(
        self, alert_id: str, name: str, status: DeliveryStatus, attempts: int, error: str
    ) -> DeliveryRecord:
        """Return a delivery row stamped with the engine's time."""
        return DeliveryRecord(alert_id, name, status, attempts, error, self.engine.clock.now())

    async def _write(self, record: DeliveryRecord) -> bool:
        """Save one delivery row; on a store error fail the engine stop and return ``False``."""
        return await self._save(Batch(deliveries=[record]))

    async def _save(self, batch: Batch) -> bool:
        """Save ``batch``; on a store error fail the engine stop and return ``False``.

        Store failures are fail-stop: the engine stops, and so does
        delivering (no more sends, acks or nacks).
        """
        if self.error is not None:
            return False
        try:
            await self.engine.writer.save(batch)
        except Exception as exc:  # noqa: BLE001 -- handed to the engine's fail path
            if self.error is None:
                self.error = exc
                logger.error("alert delivery stopped: the store failed: %r", exc)  # noqa: TRY400
                self._cancel_consumers()
                self.engine.fail_stop(exc)
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


__all__ = ["ATTEMPTS_HEADER", "REMOVED_ERROR", "Pigeonholes", "queue_config"]
