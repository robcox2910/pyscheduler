"""The hub's line to its agents: the broker connection, sealed sending and log requests.

This is the office's post room. It opens the building's own post office
(an embedded ``BrokerServer`` with ``agents: listen:``) or walks to the
one in town (``agents: broker:``); either way it talks through one
``BrokerClient``. It seals each outgoing letter with the addressee's
secret, and it runs the "phone another branch" log requests: ask, then
wait up to ``LOG_TIMEOUT`` for the matching ``log_reply``.
"""

import contextlib
import ipaddress
import logging
import secrets
from collections.abc import Coroutine, Mapping
from datetime import timedelta
from typing import Any

import pynet.loop
from pymq.broker import BrokerClient, BrokerError, BrokerServer, Delivery
from pynet.loop import Future, Task
from pynet.messaging import JsonValue

from pyscheduler.agents.log_messages import (
    LOG_MODES,
    LOG_STREAMS,
    LogReply,
    LogRequest,
    log_request_payload,
)
from pyscheduler.agents.messages import (
    LOG_REQUEST_TYPE,
    QUEUE_CONFIG,
    REPORTS_QUEUE,
    work_queue,
)
from pyscheduler.agents.signing import seal
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.config.model import AgentsDef
from pyscheduler.engine.clock import Clock

__all__ = ["LOG_TIMEOUT", "AgentOffline", "HubLink"]

logger = logging.getLogger("pyscheduler.agents.hub")

LOG_TIMEOUT = timedelta(seconds=10)
"""How long ``fetch_log`` waits for the agent's ``log_reply``."""
REQUEST_ID_BYTES = 8
ATTACH_WAIT = 10.0
"""Seconds ``_attach`` waits for the broker line each time round."""
ATTACH_RETRY = 1.0
"""Seconds ``_attach`` pauses after a failed attempt."""


class AgentOffline(Exception):
    """Raised when an agent cannot answer because it is offline (or the hub stopped)."""


class HubLink:
    """The broker side of ``AgentHub``: connect, send sealed orders, ask for logs.

    Args:
        agents: The ``agents:`` section (``listen`` or ``broker`` must be set).
        clock: The engine clock for message times and the log timeout.
        secrets: The agents' shared secrets; swap them with ``set_secrets``.

    """

    def __init__(self, agents: AgentsDef, *, clock: Clock, secrets: AgentSecrets) -> None:
        """Set the line up; nothing listens or connects until ``_connect``."""
        self.listen, self.broker = agents.listen, agents.broker
        self.clock = clock
        self._secrets = secrets
        self._server: BrokerServer | None = None
        self._client: BrokerClient | None = None
        self._address: tuple[str, int] | None = None
        self._declared: set[str] = set()
        self._tasks: set[Task[None]] = set()
        self._requests: dict[str, tuple[str, Future[LogReply]]] = {}
        self._online: dict[str, bool] = {}
        self._stopped = False
        self.attached = False
        """Whether the reports queue's consume has been set up (not whether the line is up now)."""

    def __repr__(self) -> str:
        """Show where the hub meets its agents, never a secret."""
        return f"{type(self).__name__}(listen={self.listen!r}, broker={self.broker!r})"

    def _secret_for(self, name: str) -> str | None:
        """Return agent ``name``'s secret from the current secrets, or ``None``."""
        return self._secrets.get(name)

    def set_secrets(self, secrets: AgentSecrets) -> None:
        """Use ``secrets`` from now on, for checking reports and sealing orders."""
        self._secrets = secrets

    @property
    def address(self) -> tuple[str, int]:
        """Return the broker ``(host, port)`` the hub is connected to.

        Raises:
            RuntimeError: If the hub has not started.

        """
        if self._address is None:
            msg = "the hub has not started"
            raise RuntimeError(msg)
        return self._address

    async def _on_delivery(self, delivery: Delivery) -> None:
        """Handle one report (``AgentHub`` does the work)."""
        raise NotImplementedError

    async def _connect(self) -> None:
        """Start the embedded broker (with ``listen``) and the client, then attach in the background.

        Returns without waiting for the broker: ``_attach`` declares and
        consumes the reports queue once the line is up. Until then sends
        fail and are logged, and the re-puts cover them.
        """
        address = self.broker
        if self.listen is not None:
            host, port = self.listen
            server = BrokerServer()
            await server.start(host, port)
            self._server = server
            address = (_dial_host(host), server.port)
        if address is None:  # ruled out by AgentHub.__init__
            msg = "the hub has no broker to connect to"
            raise RuntimeError(msg)
        self._address = address
        client = BrokerClient(*address)
        self._client = client
        await client.start()
        self._spawn(self._attach(client), "attach")

    async def _attach(self, client: BrokerClient) -> None:
        """Declare and consume ``scheduler.reports`` once the broker answers; retry until then.

        After the first success the client re-declares and re-consumes by
        itself on every reconnect.
        """
        while True:
            try:
                await client.wait_connected(ATTACH_WAIT)
                await client.declare_queue(REPORTS_QUEUE, QUEUE_CONFIG)
                await client.consume(REPORTS_QUEUE, self._on_delivery)
            except (BrokerError, TimeoutError) as exc:
                logger.warning("cannot reach the agents' broker yet: %s", type(exc).__name__)
                await pynet.loop.sleep(ATTACH_RETRY)
            else:
                self.attached = True
                return

    async def _hang_up(self) -> None:
        """Fail the waiting log requests, end every task, hang up and close the broker."""
        self._fail_requests(None, "the hub stopped")
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if self._client is not None:
            await self._client.close()
        if self._server is not None:
            await self._server.close()

    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Run ``work`` as a task that ``stop`` cancels."""
        task = pynet.loop.spawn(work, name=f"hub-{name}")
        self._tasks.add(task)
        task.add_done_callback(lambda _: self._tasks.discard(task))

    async def _send(
        self, agent: str, kind: str, payload: Mapping[str, JsonValue], *, msg_id: str | None = None
    ) -> None:
        """Seal ``payload`` for ``agent`` with its secret and put it on its work queue.

        A missing secret or an unreachable broker is logged (by type, never
        the payload), not raised: re-puts and repeated reports cover it.
        """
        client, secret = self._client, self._secrets.get(agent)
        if client is None or self._stopped:
            return
        if secret is None:
            logger.warning("cannot send %s to agent %s: it has no secret", kind, agent)
            return
        queue = work_queue(agent)
        try:
            if queue not in self._declared:
                await client.declare_queue(queue, QUEUE_CONFIG)
                self._declared.add(queue)
            body, headers = seal(kind, agent, payload, secret, now=self.clock.now())
            await client.put(queue, body, headers=headers, id=msg_id)
        except (BrokerError, TimeoutError) as exc:
            logger.warning("cannot send %s to agent %s: %s", kind, agent, type(exc).__name__)

    # Logs --------------------------------------------------------------

    async def fetch_log(
        self,
        agent: str,
        run_id: str,
        attempt: int,
        stream: str,
        *,
        mode: str,
        count: int | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> LogReply:
        """Ask ``agent`` for part of an attempt's log and wait for the reply (P4-R9).

        ``mode`` ``tail`` needs ``count`` lines; ``read`` needs ``limit``
        bytes from ``offset`` (default 0).

        Raises:
            AgentOffline: If the agent is offline now, or goes offline before replying.
            TimeoutError: If no reply comes within ``LOG_TIMEOUT``.
            ValueError: If the stream, mode or sizes are not valid.

        """
        if stream not in LOG_STREAMS or mode not in LOG_MODES:
            msg = f"stream must be one of {LOG_STREAMS} and mode one of {LOG_MODES}"
            raise ValueError(msg)
        if (count if mode == "tail" else limit) is None:
            msg = "a tail needs count and a read needs limit"
            raise ValueError(msg)
        if self._stopped or not self._online.get(agent, False):
            raise AgentOffline(agent)
        request_id = secrets.token_hex(REQUEST_ID_BYTES)
        request = LogRequest(
            request_id, run_id, attempt, stream, mode, count or 0, offset or 0, limit or 0
        )
        future: Future[LogReply] = Future(pynet.loop.get_running_loop())
        self._requests[request_id] = (agent, future)

        def expire() -> None:
            """Give up waiting."""
            if not future.done():
                future.set_exception(TimeoutError(f"no log reply from agent {agent}"))

        timer = self.clock.call_at(self.clock.now() + LOG_TIMEOUT, expire)
        try:
            # The send runs on its own task: a broker that is down cannot hold the
            # caller past the timeout.
            send = self._send(agent, LOG_REQUEST_TYPE, log_request_payload(request))
            self._spawn(send, f"log-{request_id}")
            return await future
        finally:
            timer.cancel()
            self._requests.pop(request_id, None)

    def _replied(self, agent: str, reply: LogReply) -> None:
        """Hand ``reply`` to the request waiting for it, if ``agent`` was the one asked."""
        entry = self._requests.get(reply.request_id)
        if entry is not None and entry[0] == agent and not entry[1].done():
            entry[1].set_result(reply)

    def _fail_requests(self, agent: str | None, why: str) -> None:
        """Raise ``AgentOffline(why)`` in the requests waiting on ``agent`` (``None``: all)."""
        for asked, future in self._requests.values():
            if agent in (None, asked) and not future.done():
                future.set_exception(AgentOffline(why))


def _dial_host(host: str) -> str:
    """Return the address to dial for a server listening on ``host``.

    A wildcard listen address (all interfaces) is reached on loopback.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.is_unspecified:
        return "::1" if isinstance(ip, ipaddress.IPv6Address) else "127.0.0.1"
    return host
