"""The agent: a worker that takes sealed orders from the broker and runs them.

Think of a builder working on a far-away site. Orders arrive by post
(the agent's ``agent.<name>.work`` box). The builder checks each
letter's wax seal (``Verifier``), writes the job in a logbook (the
``Journal``) *before* saying "got it" (the ack), and then gets on with
it, using the same minder the scheduler uses at home. News goes back in
sealed letters to ``scheduler.reports``: ``hello`` when the line comes
up, ``started``, a ``heartbeat`` every ten seconds, ``finished`` (sent
again and again until the scheduler says ``finished_ack``) and
``log_reply``.

The logbook makes repeats safe: an order for an attempt already in the
book is never run twice, it only gets its ``started`` (and ``finished``)
news again. Stopping the agent leaves its jobs running; the next start
finds them in the logbook and picks them up.
"""

import contextlib
import logging
import os
from collections.abc import Coroutine, Iterable
from pathlib import Path
from typing import Any

import pynet.loop
from pydb.errors import PyDBError
from pymq.broker import BrokerClient, BrokerError, Delivery
from pynet.loop import Future, Task
from pynet.messaging import ConnectionEvent, JsonValue

from pyscheduler.agents.journal import RUNNING, Entry, Journal, Report
from pyscheduler.agents.log_messages import LogRequest, log_reply_payload, parse_log_request
from pyscheduler.agents.messages import (
    ACK_TYPE,
    FINISHED_TYPE,
    HEARTBEAT_EVERY,
    HEARTBEAT_TYPE,
    HELLO_TYPE,
    KILL_TYPE,
    LOG_REPLY_TYPE,
    QUEUE_CONFIG,
    REPORTS_QUEUE,
    SCHEDULER_TYPES,
    STARTED_TYPE,
    WORK_TYPE,
    RunKey,
    WorkOrder,
    finished_payload,
    heartbeat_payload,
    hello_payload,
    parse_key,
    parse_kill,
    parse_work,
    started_payload,
    work_queue,
)
from pyscheduler.agents.signing import Message, Rejected, Verifier, seal
from pyscheduler.agents.worker_files import attempt_files, look, read_log
from pyscheduler.engine.adopted import AdoptedHandle
from pyscheduler.engine.clock import Clock
from pyscheduler.engine.launcher import AttemptHandle, LaunchSpec, MinderLauncher
from pyscheduler.engine.recovery import ADOPT_POLL
from pyscheduler.engine.status_note import AttemptResult

__all__ = ["ADOPT_POLL", "MINDER_GONE", "Agent"]

MINDER_GONE = "agent restarted; minder gone"
"""The start error of an attempt whose minder vanished without a note while the agent was down."""

logger = logging.getLogger("pyscheduler.agents.worker")


class Agent:
    """A ``pysched-agent`` worker, run on the pynet loop.

    Args:
        name: The agent's name (its work queue is ``agent.<name>.work``).
        tags: What this agent offers, for jobs placed by ``agent_tags``.
        secret: The secret shared with the scheduler; never logged or shown.
        broker: The broker's ``(host, port)``.
        state_dir: Where the journal and the minders' status notes live.
        log_dir: Where the jobs' logs go.
        max_running: How many attempts the scheduler may give it at once.
        clock: The clock for message times, heartbeats and adoption polls.

    """

    def __init__(
        self,
        *,
        name: str,
        tags: Iterable[str],
        secret: str,
        broker: tuple[str, int],
        state_dir: Path,
        log_dir: Path,
        max_running: int,
        clock: Clock,
    ) -> None:
        """Set the agent up; nothing is opened or sent until ``start``."""
        self.name = name
        self.tags = frozenset(tags)
        self._secret = secret
        self.broker = broker
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.max_running = max_running
        self.clock = clock
        self._verifier = Verifier(self._secret_for, clock=clock, accept=SCHEDULER_TYPES)
        self._launcher = MinderLauncher()
        self._journal: Journal | None = None
        self._client: BrokerClient | None = None
        self._handles: dict[RunKey, AttemptHandle] = {}
        self._pending: dict[RunKey, Report] = {}
        self._tasks: set[Task[None]] = set()
        self._stopped = False

    def __repr__(self) -> str:
        """Show the name and broker, never the secret."""
        host, port = self.broker
        return f"Agent(name={self.name!r}, broker='{host}:{port}')"

    @property
    def running(self) -> frozenset[RunKey]:
        """Return the attempts running here now."""
        return frozenset(self._handles)

    @property
    def pending(self) -> frozenset[RunKey]:
        """Return the attempts whose ``finished`` report the scheduler has not acked."""
        return frozenset(self._pending)

    def _secret_for(self, name: str) -> str | None:
        """Return the secret for orders addressed to this agent; any other name is unknown."""
        return self._secret if name == self.name else None

    async def start(self) -> None:
        """Open the journal, pick up what it says was running, connect, consume and say hello.

        Waits for the broker as long as it takes to answer the first time.
        """
        journal = await pynet.loop.run_in_thread(Journal.open, self.state_dir)
        self._journal = journal
        self._pending = await pynet.loop.run_in_thread(journal.pending)
        for entry in await pynet.loop.run_in_thread(journal.running):
            await self._pick_up(entry)
        client = BrokerClient(*self.broker)
        self._client = client
        client.on_event(self._on_event)
        await client.start()
        await client.wait_connected()
        await client.declare_queue(REPORTS_QUEUE, QUEUE_CONFIG)
        await client.declare_queue(work_queue(self.name), QUEUE_CONFIG)
        await client.consume(work_queue(self.name), self._on_delivery, auto_ack=False)
        await self._greet()
        self._spawn(self._beat(), "heartbeat")

    async def stop(self) -> None:
        """Hang up and close the journal; running jobs carry on (safe to call twice).

        The next ``start`` on the same state folder adopts them.
        """
        if self._stopped:
            return
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        if self._client is not None:
            await self._client.close()
        if self._journal is not None:
            await pynet.loop.run_in_thread(self._journal.close)

    def _book(self) -> Journal:
        """Return the open journal.

        Raises:
            RuntimeError: If the agent has not started.

        """
        if self._journal is None:
            msg = "the agent has not started"
            raise RuntimeError(msg)
        return self._journal

    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Run ``work`` as a task the agent cancels on ``stop``; log it if it fails."""
        task = pynet.loop.spawn(work, name=f"agent-{self.name}-{name}")
        self._tasks.add(task)

        def done(finished: Future[None]) -> None:
            """Forget the task, logging an unexpected failure."""
            self._tasks.discard(task)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                logger.error("agent task %s failed: %s", name, error, exc_info=error)

        task.add_done_callback(done)

    # Sending -----------------------------------------------------------

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
        """Every ``HEARTBEAT_EVERY``, say what runs and send unacked reports again."""
        while True:
            await self.clock.sleep_until(self.clock.now() + HEARTBEAT_EVERY)
            await self._send(HEARTBEAT_TYPE, heartbeat_payload(self._handles))
            await self._resend()

    async def _send_started(self, entry: Entry) -> None:
        """Send the ``started`` report of ``entry`` with its log paths."""
        out, err = str(entry.stdout_path), str(entry.stderr_path)
        await self._send(STARTED_TYPE, started_payload(entry.run_id, entry.attempt, out, err))

    def _on_event(self, event: ConnectionEvent) -> None:
        """After a reconnect, say hello again and re-send the unacked reports."""
        if event is ConnectionEvent.RECONNECTED and not self._stopped:
            self._spawn(self._greet(), "greet")

    # Receiving ---------------------------------------------------------

    async def _on_delivery(self, delivery: Delivery) -> None:
        """Check one order and act on it; a refused order is logged and dropped.

        The log line holds the reason and the delivery id only, never the
        secret or the payload. A work order is acked by ``_work`` itself,
        once the journal has it.
        """
        try:
            message = self._verifier.check(delivery.body, delivery.headers)
            if message.kind == WORK_TYPE:
                await self._work(delivery, parse_work(message.payload))
                return
            await self._order(message)
        except Rejected as exc:
            logger.warning("rejected a message (delivery %s): %s", delivery.id, exc.reason)
        await delivery.ack()

    async def _order(self, message: Message) -> None:
        """Act on a checked ``finished_ack``, ``kill`` or ``log_request``."""
        if message.kind == ACK_TYPE:
            await self._acked(parse_key(message.payload))
        elif message.kind == KILL_TYPE:
            self._kill(*parse_kill(message.payload))
        else:
            await self._log(parse_log_request(message.payload))

    async def _work(self, delivery: Delivery, order: WorkOrder) -> None:
        """Journal ``order``, ack it, then run it, or only repeat the news if it is known.

        If the journal cannot record it, the order is dead-lettered unacted;
        the scheduler sends it again until it hears ``started``.
        """
        out, err, status = attempt_files(order, self.log_dir, self.state_dir)
        fresh = Entry(order.run_id, order.attempt, RUNNING, None, out, err, status)
        try:
            entry, new = await pynet.loop.run_in_thread(self._book().accept, fresh)
        except PyDBError, OSError:
            logger.exception("cannot journal work %s.%d; dropping it", *order.key)
            await delivery.nack(requeue=False)
            return
        # The journal has it now: a failed ack must not stop the launch, or the
        # attempt would sit in the journal as running with no minder behind it.
        with contextlib.suppress(BrokerError, TimeoutError):
            await delivery.ack()
        if not new:
            await self._send_started(entry)
            if entry.finished is not None:
                await self._send(FINISHED_TYPE, entry.finished)
            return
        await self._launch(order, entry)

    async def _launch(self, order: WorkOrder, entry: Entry) -> None:
        """Start ``order`` under the minder, report ``started`` and watch for its end."""
        spec = LaunchSpec(
            run_id=order.run_id,
            attempt=order.attempt,
            argv=order.argv,
            cwd=order.cwd,
            env={**os.environ, **order.env},
            stdout_path=entry.stdout_path,
            stderr_path=entry.stderr_path,
            status_path=entry.status_path,
        )
        try:
            handle = await self._launcher.launch(spec)
        except (OSError, ValueError, TypeError) as exc:
            await self._send_started(entry)
            failed = AttemptResult(None, None, None, self.clock.now(), str(exc))
            await self._finish(entry.key, failed)
            return
        self._handles[entry.key] = handle
        await self._send_started(entry)
        self._spawn(self._watch(entry.key, handle), f"watch-{entry.run_id}.{entry.attempt}")

    async def _watch(self, key: RunKey, handle: AttemptHandle) -> None:
        """Wait for the attempt to end, then journal and report it."""
        result = await handle.wait()
        if isinstance(handle, AdoptedHandle) and handle.lost:
            result = AttemptResult(None, None, None, result.ended_at, MINDER_GONE)
        await self._finish(key, result)

    async def _finish(self, key: RunKey, result: AttemptResult) -> None:
        """Journal how ``key`` ended, keep the report pending, and send it."""
        report = finished_payload(key[0], key[1], result)
        try:
            await pynet.loop.run_in_thread(self._book().finish, key, report)
        except PyDBError, OSError:
            logger.exception("cannot journal the end of %s.%d", *key)
        self._pending[key] = report
        self._handles.pop(key, None)
        await self._send(FINISHED_TYPE, report)

    async def _acked(self, key: RunKey) -> None:
        """Cross ``key``'s report off: it is never sent again."""
        if self._pending.pop(key, None) is not None:
            await pynet.loop.run_in_thread(self._book().acked, key)

    def _kill(self, run_id: str, attempt: int, signal: str) -> None:
        """Send ``TERM`` or ``KILL`` to the attempt's process group, if it runs here."""
        handle = self._handles.get((run_id, attempt))
        if handle is None:
            logger.info("kill for %s.%d, which is not running here", run_id, attempt)
        elif signal == "TERM":
            handle.terminate()
        else:
            handle.kill()

    async def _log(self, request: LogRequest) -> None:
        """Read the asked-for part of a log on a thread and reply."""
        key = (request.run_id, request.attempt)
        entry = await pynet.loop.run_in_thread(self._book().get, key)
        reply = await pynet.loop.run_in_thread(read_log, entry, request)
        await self._send(LOG_REPLY_TYPE, log_reply_payload(reply))

    # Restart -----------------------------------------------------------

    async def _pick_up(self, entry: Entry) -> None:
        """Deal with an attempt the journal says was running when the agent last stopped.

        A note means it ended; a live minder is adopted; otherwise the
        attempt is reported finished with ``MINDER_GONE``.
        """
        found = await pynet.loop.run_in_thread(look, entry.status_path)
        if found.result is not None:
            await self._finish(entry.key, found.result)
        elif found.pid is not None and found.started_at is not None:
            handle = AdoptedHandle(
                self.clock, found.pid, found.started_at, entry.status_path, poll=ADOPT_POLL
            )
            self._handles[entry.key] = handle
            self._spawn(self._watch(entry.key, handle), f"adopt-{entry.run_id}.{entry.attempt}")
        else:
            gone = AttemptResult(None, None, None, self.clock.now(), MINDER_GONE)
            await self._finish(entry.key, gone)
