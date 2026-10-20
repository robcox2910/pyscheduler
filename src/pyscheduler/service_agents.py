"""The agent side of ``serve``: the hub, the engine's placement and the agents' live news.

When the config has an ``agents:`` section with ``listen`` or ``broker``,
the shop has a **dispatch office** for its far-away crews (the
``AgentHub``). It is set up before the staff (the engine) so the engine
can be told where remote runs go, and opened before the staff start work,
so the crews' news is heard while recovery looks for jobs they hold. At
closing time the staff go home first, then the office shuts: an attempt
still out with a crew carries on there, and the next start adopts it.

Without the section there is no office: everything here does nothing.
"""

from pymq.broker import BrokerError
from pynet.errors import PyNetError

from pyscheduler.agents.hub import AgentHub
from pyscheduler.agents.placement import HubPlacement
from pyscheduler.config import Config
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.engine.clock import Clock
from pyscheduler.service_parts import ServiceError, logger
from pyscheduler.ui.live import LiveFeed

__all__ = ["ServiceAgents"]


class ServiceAgents:
    """Hold the agent hub of one ``serve`` call (or nothing, when agents are off).

    Args:
        config: The config ``serve`` loaded.
        clock: The clock for the engine and the hub (re-puts and countdowns).
        live: The live feed that gets an ``agent`` event whenever an agent
            changes, until ``stop``.

    """

    def __init__(self, config: Config, clock: Clock, live: LiveFeed) -> None:
        """Build the hub and its placement if ``agents:`` sets ``listen`` or ``broker``."""
        agents = config.agents
        self.clock = clock
        self.hub: AgentHub | None = None
        self.placement: HubPlacement | None = None
        if agents is not None and (agents.listen is not None or agents.broker is not None):
            self.hub = AgentHub(config, clock=clock, secrets=agents.secrets)
            self.placement = HubPlacement(self.hub)
        self._detach = None if self.hub is None else live.attach_agents(self.hub.book)

    def set_secrets(self, secrets: AgentSecrets) -> None:
        """Use the reloaded ``secrets`` from now on (nothing without agents)."""
        if self.hub is not None:
            self.hub.set_secrets(secrets)

    async def start(self) -> None:
        """Start the hub: the embedded broker listens, or the client dials the outside one.

        Raises:
            ServiceError: If the embedded broker cannot listen (a busy port, say).

        """
        if self.hub is None:
            return
        try:
            await self.hub.start()
        except (PyNetError, OSError, BrokerError) as exc:
            cause = exc.__cause__ if isinstance(exc, PyNetError) and exc.__cause__ else exc
            host, port = self.hub.listen or self.hub.broker or ("?", 0)
            msg = f"agents: cannot start the broker on {host}:{port}: {cause}"
            raise ServiceError(msg) from None
        host, port = self.hub.address
        logger.info("agents meet on the broker at %s:%d", host, port)

    async def stop(self) -> None:
        """Stop the live news and the hub (after the engine); safe if it never started."""
        if self._detach is not None:
            self._detach()
            self._detach = None
        if self.hub is not None:
            await self.hub.stop()
