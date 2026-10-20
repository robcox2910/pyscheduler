"""The scheduler's agent hub: the dispatch office between the engine and its agents.

The hub is the office of a building firm that sends crews out to far-away
sites. It posts sealed orders into each crew's mailbox (a ``work`` letter
per attempt, posted again every ``agent_grace`` until the crew says it
has ``started``), reads the crews' sealed news from the one shared
``scheduler.reports`` box, keeps the sign-in sheet (the ``AgentBook``) up
to date, and fills in each attempt's tracking page (its
``RemoteHandle``). A letter whose seal does not fit is logged and thrown
away. When a crew goes silent, every job it holds gets a ``lost_after``
countdown; any news about a job stops its countdown.

The broker connection, sealing and log requests live in ``HubLink``. All
timing goes through the engine ``Clock``.
"""

import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime

from pymq.broker import Delivery
from pynet.messaging import JsonValue

from pyscheduler.agents.book import AgentBook, AgentState
from pyscheduler.agents.hub_link import LOG_TIMEOUT, AgentOffline, HubLink
from pyscheduler.agents.log_messages import parse_log_reply
from pyscheduler.agents.messages import (
    ACK_TYPE,
    FINISHED_TYPE,
    HEARTBEAT_TYPE,
    HELLO_TYPE,
    KILL_TYPE,
    REPORT_TYPES,
    STARTED_TYPE,
    WORK_TYPE,
    RunKey,
    key_payload,
    kill_payload,
    message_id,
    parse_finished,
    parse_heartbeat,
    parse_hello,
    parse_key,
    parse_started,
)
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.agents.signing import Message, Rejected, Verifier
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.config.model import Config
from pyscheduler.engine.clock import Cancel, Clock
from pyscheduler.engine.status_note import AttemptResult

__all__ = ["DONE_MEMORY", "LOG_TIMEOUT", "AgentHub", "AgentOffline", "RemoteHandle"]

logger = logging.getLogger("pyscheduler.agents.hub")

DONE_MEMORY = 10_000
"""How many settled attempts are remembered, so a repeated ``finished`` is acked again."""


class AgentHub(HubLink):
    """Dispatch work to agents over a broker and follow their reports.

    Args:
        config: A config with an ``agents:`` section (``listen`` or ``broker``).
        clock: The engine clock for message times, re-puts and countdowns.
        secrets: The agents' shared secrets; swap them with ``set_secrets``.

    Raises:
        ValueError: If the config has no usable ``agents:`` section.

    """

    def __init__(self, config: Config, *, clock: Clock, secrets: AgentSecrets) -> None:
        """Set the hub up; nothing listens or connects until ``start``."""
        agents = config.agents
        if agents is None or (agents.listen is None and agents.broker is None):
            msg = "the hub needs an agents: section with listen or broker"
            raise ValueError(msg)
        super().__init__(agents, clock=clock, secrets=secrets)
        self.grace, self.lost_after = agents.agent_grace, agents.lost_after
        self.book = AgentBook(clock=clock, grace=self.grace)
        self._verifier = Verifier(self._secret_for, clock=clock, accept=REPORT_TYPES)
        self._handles: dict[RunKey, RemoteHandle] = {}
        self._work: dict[RunKey, dict[str, JsonValue]] = {}
        self._done: OrderedDict[RunKey, str] = OrderedDict()
        self._lost_timers: dict[RunKey, Cancel] = {}
        self._reputs: dict[RunKey, Cancel] = {}
        self._started_watchers: list[Callable[[RemoteHandle], None]] = []
        self._recovered = False
        self._unwatch = self.book.watch(self._on_agent)

    # Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the embedded broker (with ``listen``) and the client, and return.

        The reports queue is declared and consumed in the background once
        the broker answers (``attached`` turns true); ``start`` never waits
        for an outside broker that is down.
        """
        await self._connect()

    async def stop(self) -> None:
        """Cancel every timer and task, hang up and close the embedded broker (idempotent).

        Unresolved handles stay unresolved: their attempts carry on at the
        agents, and recovery adopts them on the next start.
        """
        if self._stopped:
            return
        self._stopped = True
        self._unwatch()
        self.book.close()
        for timer in [*self._lost_timers.values(), *self._reputs.values()]:
            timer.cancel()
        self._lost_timers.clear()
        self._reputs.clear()
        await self._hang_up()

    def recovered(self) -> None:
        """Note that recovery has adopted every remote attempt it will follow.

        From now on a ``finished`` for an attempt nobody follows is acked
        and dropped, so the agent stops sending it.
        """
        self._recovered = True

    def watch_started(self, callback: Callable[[RemoteHandle], None]) -> Callable[[], None]:
        """Call ``callback`` with the handle when an attempt's first ``started`` arrives.

        Returns:
            A function that stops the watching.

        """
        self._started_watchers.append(callback)

        def stop() -> None:
            """Remove the callback if it is still registered."""
            if callback in self._started_watchers:
                self._started_watchers.remove(callback)

        return stop

    # Dispatch and adoption ---------------------------------------------

    def dispatch(self, agent: str, payload: Mapping[str, JsonValue]) -> RemoteHandle:
        """Seal and put ``payload`` (a work payload) on ``agent``'s queue; return its handle.

        The work is put again, with the same message id, every
        ``agent_grace`` until ``started`` arrives (P4-R4); with no report
        at all within ``lost_after`` the attempt is lost. The slot is held
        in the book until the attempt ends. Dispatching an attempt that is
        already followed returns its handle.

        Raises:
            ValueError: If ``payload`` has no valid ``run_id`` and ``attempt``.
            RuntimeError: If the hub has stopped.

        """
        key = self._key(payload)
        if (known := self._handles.get(key)) is not None:
            return known
        handle = self._follow(agent, key, None)
        self._work[key] = dict(payload)
        self._put_work(key)
        self._arm_lost(handle, self.clock.now() + self.lost_after)
        return handle

    def adopt(self, agent: str, run_id: str, attempt: int, started_at: datetime) -> RemoteHandle:
        """Follow an attempt recovery found running on ``agent``; return its handle.

        The ``lost_after`` countdown starts now (P4-R8), whether or not the
        agent is online; any report for the attempt (``started``,
        ``finished``, or a heartbeat that lists it) stops it.

        Raises:
            RuntimeError: If the hub has stopped.

        """
        key = (run_id, attempt)
        if (known := self._handles.get(key)) is not None:
            return known
        handle = self._follow(agent, key, started_at)
        self._arm_lost(handle, self.clock.now() + self.lost_after)
        return handle

    @staticmethod
    def _key(payload: Mapping[str, JsonValue]) -> RunKey:
        """Return the attempt a work payload names, or raise ``ValueError``."""
        try:
            return parse_key(payload)
        except Rejected:
            msg = "a work payload needs a valid run_id and attempt"
            raise ValueError(msg) from None

    def _follow(self, agent: str, key: RunKey, started_at: datetime | None) -> RemoteHandle:
        """Make, remember and reserve a slot for the handle of ``key`` on ``agent``."""
        if self._stopped:
            msg = "the hub has stopped"
            raise RuntimeError(msg)
        handle = RemoteHandle(agent, *key, signal=self._send_kill, started_at=started_at)
        self._handles[key] = handle
        self.book.reserve(agent, key)
        return handle

    def _put_work(self, key: RunKey) -> None:
        """Put ``key``'s work on its agent's queue, and arm the next re-put."""
        handle, payload = self._handles.get(key), self._work.get(key)
        self._reputs.pop(key, None)
        if handle is None or payload is None or self._stopped:
            return
        msg_id = message_id(*key)
        self._spawn(self._send(handle.agent, WORK_TYPE, payload, msg_id=msg_id), f"work-{msg_id}")
        when = self.clock.now() + self.grace
        self._reputs[key] = self.clock.call_at(when, lambda: self._put_work(key))

    def _send_kill(self, handle: RemoteHandle, signal: str) -> None:
        """Send a signed ``kill`` for ``handle`` on a task (nothing once stopped)."""
        if self._stopped:
            return
        payload = kill_payload(handle.run_id, handle.attempt, signal)
        self._spawn(self._send(handle.agent, KILL_TYPE, payload), f"kill-{handle.run_id}")

    # Reports -----------------------------------------------------------

    async def _on_delivery(self, delivery: Delivery) -> None:
        """Check one report and act on it; anything refused is logged and dropped.

        The log line holds the reason and the delivery id only, never a
        secret or the payload. The delivery is always acked (on return).
        Once the hub is stopping, reports are dropped unread: the agents
        send them again.
        """
        if self._stopped:
            return
        try:
            self._act(self._verifier.check(delivery.body, delivery.headers))
        except Rejected as exc:
            logger.warning("rejected a report (delivery %s): %s", delivery.id, exc.reason)
        except Exception as exc:  # never let one report stop the rest; never log its payload
            logger.error(
                "cannot handle a report (delivery %s): %s", delivery.id, type(exc).__name__
            )

    def _act(self, message: Message) -> None:
        """Parse a checked report, note the agent as heard, and act on it."""
        agent, kind, payload = message.agent, message.kind, message.payload
        if kind == HELLO_TYPE:
            tags, max_running, running = parse_hello(payload)
            self.book.heard(agent, tags=tags, max_running=max_running, running=running)
            self._seen(agent, running)
        elif kind == HEARTBEAT_TYPE:
            running = parse_heartbeat(payload)
            self.book.heard(agent, running=running)
            self._seen(agent, running)
        elif kind == STARTED_TYPE:
            key, out, err = parse_started(payload)
            self.book.heard(agent)
            self._started(agent, key, out, err)
        elif kind == FINISHED_TYPE:
            key, result = parse_finished(payload)
            self.book.heard(agent)
            self._finished(agent, key, result)
        else:
            reply = parse_log_reply(payload)
            self.book.heard(agent)
            self._replied(agent, reply)

    def _ours(self, agent: str, key: RunKey) -> RemoteHandle | None:
        """Return the handle of ``key`` if ``agent`` is the one running it."""
        handle = self._handles.get(key)
        return handle if handle is not None and handle.agent == agent else None

    def _seen(self, agent: str, running: set[RunKey]) -> None:
        """Stop the countdown and re-puts of every attempt ``agent`` says it runs.

        A started attempt the agent no longer lists gets a ``lost_after``
        countdown (if none runs): its ``finished`` should be on the way,
        and if it never comes the attempt is lost.
        """
        for handle in [h for h in self._handles.values() if h.agent == agent]:
            if handle.key in running:
                self._quiet(handle.key)
            elif handle.stdout_path is not None and handle.key not in self._lost_timers:
                self._arm_lost(handle, self.clock.now() + self.lost_after)

    def _quiet(self, key: RunKey) -> None:
        """Cancel ``key``'s lost countdown and its re-puts."""
        for timers in (self._lost_timers, self._reputs):
            if (timer := timers.pop(key, None)) is not None:
                timer.cancel()

    def _started(self, agent: str, key: RunKey, out: str, err: str) -> None:
        """Record the log paths of a followed attempt; tell the watchers the first time."""
        handle = self._ours(agent, key)
        if handle is None:
            logger.info("started from %s for %s.%d, which it does not follow", agent, *key)
            return
        self._quiet(key)
        if handle.record_start(out, err):
            for callback in list(self._started_watchers):
                try:
                    callback(handle)
                except Exception:
                    logger.exception("started watcher failed")

    def _finished(self, agent: str, key: RunKey, result: AttemptResult) -> None:
        """Resolve the attempt and ack the report; ack a repeat; handle an unknown one.

        Before ``recovered()``, an unknown attempt's report is not acked, so
        the agent keeps sending it until recovery adopts the attempt. After
        it, no run will ever follow that attempt, so the report is acked and
        dropped (unless another agent runs it: a rival's claim is never acked).
        """
        handle = self._ours(agent, key)
        if handle is None:
            if self._done.get(key) == agent:
                self._ack(agent, key)
            elif self._recovered and key not in self._handles:  # never ack a rival's claim
                logger.info(
                    "finished from %s for %s.%d, which no run follows: acked and dropped",
                    agent,
                    *key,
                )
                self._ack(agent, key)
            else:
                logger.info("finished from %s for %s.%d, which it does not follow", agent, *key)
            return
        self._settle(handle, result, lost=False)
        self._ack(agent, key)

    def _ack(self, agent: str, key: RunKey) -> None:
        """Send ``finished_ack`` for ``key`` on a task."""
        self._spawn(self._send(agent, ACK_TYPE, key_payload(*key)), f"ack-{key[0]}")

    def _settle(self, handle: RemoteHandle, result: AttemptResult, *, lost: bool) -> None:
        """Stop following ``handle``, free its slot and resolve it."""
        key = handle.key
        self._quiet(key)
        self._handles.pop(key, None)
        self._work.pop(key, None)
        self._done[key] = handle.agent
        while len(self._done) > DONE_MEMORY:
            self._done.popitem(last=False)
        self.book.release(handle.agent, key)
        handle.resolve(result, lost=lost)

    # Liveness ----------------------------------------------------------

    def _on_agent(self, state: AgentState) -> None:
        """When an agent goes offline, start its attempts' countdowns and fail its log requests.

        Each countdown runs from ``went_offline``. One that is already
        running (from dispatch, adoption or a heartbeat that dropped the
        attempt, with no report since) is kept: it was armed while the agent
        was online, so before ``went_offline``, and it always ends at or
        before ``went_offline + lost_after``; re-arming would only extend it.
        """
        was = self._online.get(state.name, False)
        self._online[state.name] = state.online
        if not was or state.online:
            return
        since = state.went_offline or self.clock.now()
        for handle in [h for h in self._handles.values() if h.agent == state.name]:
            if handle.key not in self._lost_timers:  # a countdown already running keeps going
                self._arm_lost(handle, since + self.lost_after)
        self._fail_requests(state.name, state.name)

    def _arm_lost(self, handle: RemoteHandle, when: datetime) -> None:
        """(Re)start ``handle``'s countdown to ``when``."""
        key = handle.key
        if (timer := self._lost_timers.pop(key, None)) is not None:
            timer.cancel()
        self._lost_timers[key] = self.clock.call_at(when, lambda: self._lose(key))

    def _lose(self, key: RunKey) -> None:
        """Resolve ``key`` as lost: its countdown ran out."""
        self._lost_timers.pop(key, None)
        handle = self._handles.get(key)
        if handle is None or self._stopped:
            return
        logger.warning("attempt %s.%d on agent %s is lost", *key, handle.agent)
        self._settle(handle, AttemptResult(None, None, None, self.clock.now(), None), lost=True)
        # If it is in fact still running there, stop it before the engine's retry starts.
        self._send_kill(handle, "TERM")
