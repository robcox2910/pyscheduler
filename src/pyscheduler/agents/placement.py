"""The engine's view of the agent hub: the ``Placement`` it asks where remote runs go.

The hub already keeps the sign-in sheet (its ``AgentBook``) and sends the
work. ``HubPlacement`` is the window between the two desks: the engine asks
through it which agent has room, books and frees seats, sends work and
follows attempts, without the hub having to know about jobs.
"""

from collections.abc import Callable, Mapping
from datetime import datetime

from pynet.messaging import JsonValue

from pyscheduler.agents.book import AgentState, Refusal, RunKey
from pyscheduler.agents.hub import AgentHub
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.config import JobDef

__all__ = ["HubPlacement"]


class HubPlacement:
    """Give the engine the ``Placement`` it needs from an ``AgentHub``.

    Args:
        hub: The hub that places, sends and follows the work.

    """

    def __init__(self, hub: AgentHub) -> None:
        """Wrap ``hub``."""
        self.hub = hub

    def choose(self, job: JobDef) -> str | Refusal:
        """Return the agent with room for ``job``, or why none fits."""
        return self.hub.book.choose(agent=job.agent, tags=frozenset(job.agent_tags))

    def reserve(self, agent: str, key: RunKey) -> None:
        """Hold a slot on ``agent`` for ``key`` (idempotent)."""
        self.hub.book.reserve(agent, key)

    def release(self, agent: str, key: RunKey) -> None:
        """Free the slot ``key`` holds on ``agent`` (idempotent)."""
        self.hub.book.release(agent, key)

    def watch(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` whenever an agent changes (capacity may have freed).

        Returns:
            A function that stops the watching.

        """

        def changed(_: AgentState) -> None:
            """Pass the news on without the state."""
            callback()

        return self.hub.book.watch(changed)

    def watch_started(self, callback: Callable[[RemoteHandle], None]) -> Callable[[], None]:
        """Call ``callback`` with a handle when its attempt first starts; return the unsubscribe."""
        return self.hub.watch_started(callback)

    def dispatch(self, agent: str, payload: Mapping[str, JsonValue]) -> RemoteHandle:
        """Send ``payload`` to ``agent`` through the hub (see ``AgentHub.dispatch``)."""
        return self.hub.dispatch(agent, payload)

    def adopt(self, agent: str, run_id: str, attempt: int, started_at: datetime) -> RemoteHandle:
        """Follow an attempt found running on ``agent`` (see ``AgentHub.adopt``)."""
        return self.hub.adopt(agent, run_id, attempt, started_at)

    def recovered(self) -> None:
        """Tell the hub recovery is done (see ``AgentHub.recovered``)."""
        self.hub.recovered()
