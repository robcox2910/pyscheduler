"""The agent's outgoing half: sealing and posting its news to the scheduler.

Every letter is sealed afresh when it is posted (a new nonce each time),
because the scheduler remembers nonces and would refuse a copy. News that
must not be lost (``finished``) is kept in ``pending`` and posted again
on every heartbeat and every reconnect until the scheduler acks it.
"""

import contextlib
import logging
from collections.abc import Coroutine
from typing import Any

import pynet.loop
from pymq.broker import BrokerClient, BrokerError
from pynet.loop import Future, Task
from pynet.messaging import ConnectionEvent, JsonValue

from pyscheduler.agents.journal import Entry, Report
from pyscheduler.agents.messages import (
    FINISHED_TYPE,
    HEARTBEAT_EVERY,
    HEARTBEAT_TYPE,
    HELLO_TYPE,
    REPORTS_QUEUE,
    STARTED_TYPE,
    RunKey,
    heartbeat_payload,
    hello_payload,
    started_payload,
)
from pyscheduler.agents.signing import seal
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.launcher import AttemptHandle

__all__ = ["Reporter"]

logger = logging.getLogger("pyscheduler.agents.worker")


class Reporter:
    """Post an agent's reports and run its background tasks (the base of ``Agent``)."""

    def __init__(
        self, *, name: str, tags: frozenset[str], secret: str, max_running: int, clock: Clock
    ) -> None:
        """Start with no line, nothing running and nothing pending."""
        self.name = name
        self.tags = tags
        self._secret = secret
        self.max_running = max_running
        self.clock = clock
        self._client: BrokerClient | None = None
        self._handles: dict[RunKey, AttemptHandle] = {}
        self._pending: dict[RunKey, Report] = {}
        self._tasks: set[Task[None]] = set()
        self._stopped = False

    @property
    def running(self) -> frozenset[RunKey]:
        """Return the attempts running here now."""
        return frozenset(self._handles)

    @property
    def pending(self) -> frozenset[RunKey]:
        """Return the attempts whose ``finished`` report the scheduler has not acked."""
        return frozenset(self._pending)

    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Run ``work`` as a task the agent cancels on ``stop``; log it if it fails."""
        task = pynet.loop.spawn(work, name=f"agent-{self.name}-{name}")
        self._tasks.add(task)

        def done(finished: Future[None]) -> None:
            """Forget the task, logging an unexpected failure."""
            self._tasks.discard(task)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                logger.error("agent task %s failed: %s", name, type(error).__name__)

        task.add_done_callback(done)

    async def _end_tasks(self) -> None:
        """Cancel every background task and wait until each has ended."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    async def _send(self, kind: str, payload: dict[str, JsonValue]) -> None:
        """Seal ``payload`` afresh (a new nonce every time) and post it to the scheduler.

        A broker that cannot be reached is logged, not raised: heartbeats,
        reconnects and repeat orders send the news again.
        """
        client = self._client
        if client is None or self._stopped:
            return
        body, headers = seal(kind, self.name, payload, self._secret, now=self.clock.now())
        try:
            await client.put(REPORTS_QUEUE, body, headers=headers)
        except (BrokerError, TimeoutError) as exc:
            logger.warning("cannot send %s to the scheduler: %s", kind, exc)

    async def _greet(self) -> None:
        """Say hello (tags, capacity, what runs) and send every unacked report again."""
        await self._send(HELLO_TYPE, hello_payload(self.tags, self.max_running, self._handles))
        await self._resend()

    async def _resend(self) -> None:
        """Send every unacked ``finished`` report again."""
        for report in list(self._pending.values()):
            await self._send(FINISHED_TYPE, report)

    async def _beat(self) -> None:
        """Every ``HEARTBEAT_EVERY``, say what runs (and the capacity) and re-send reports.

        The tags and ``max_running`` ride along on every beat, so a scheduler
        that restarted behind an outside broker (and so got no ``hello``)
        still knows what this agent offers.

        A beat that fails unexpectedly is logged and the next one still comes.
        """
        while True:
            await self.clock.sleep_until(self.clock.now() + HEARTBEAT_EVERY)
            try:
                beat = heartbeat_payload(
                    self._handles, tags=self.tags, max_running=self.max_running
                )
                await self._send(HEARTBEAT_TYPE, beat)
                await self._resend()
            except Exception:  # the heartbeat must outlive any one failure
                logger.exception("heartbeat failed")

    async def _send_started(self, entry: Entry) -> None:
        """Send the ``started`` report of ``entry`` with its log paths."""
        out, err = str(entry.stdout_path), str(entry.stderr_path)
        await self._send(STARTED_TYPE, started_payload(entry.run_id, entry.attempt, out, err))

    def _on_event(self, event: ConnectionEvent) -> None:
        """After a reconnect, say hello again and re-send the unacked reports."""
        if event is ConnectionEvent.RECONNECTED and not self._stopped:
            self._spawn(self._greet(), "greet")
