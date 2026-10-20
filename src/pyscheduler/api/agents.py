"""The agents endpoint: ``GET /api/agents``, who is online and how busy each agent is.

This is the courier firm's sign-in sheet pinned up at the front desk:
anyone with a pass may read it. It shows each agent's name, whether it is
online, its tags, how many attempts it may run at once, the attempts it
holds now and when it last spoke. It never shows a secret.
"""

from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest

from pyscheduler.agents.book import AgentState
from pyscheduler.api.serialize import instant
from pyscheduler.api.support import guarded
from pyscheduler.engine.core import Engine

__all__ = ["add_agent_routes", "agent_json", "agent_states"]

type JsonObject = dict[str, JsonValue]


def agent_json(state: AgentState) -> JsonObject:
    """Return one agent as JSON: its running attempts sorted, ``last_seen`` in UTC."""
    tags: list[JsonValue] = [*sorted(state.tags)]
    running: list[JsonValue] = [
        {"run_id": run_id, "attempt": attempt} for run_id, attempt in sorted(state.running)
    ]
    return {
        "name": state.name,
        "online": state.online,
        "tags": tags,
        "max_running": state.max_running,
        "running": running,
        "last_seen": instant(state.last_seen),
    }


def agent_states(engine: Engine) -> list[AgentState]:
    """Return every agent the engine's placement knows, by name (none when agents are off)."""
    placement = engine.placement
    return [] if placement is None else placement.states()


def add_agent_routes(app: App, engine: Engine) -> None:
    """Register ``GET /api/agents`` (an empty list when agents are not set up)."""

    @app.get("/api/agents")
    @guarded
    async def list_agents(request: HttpRequest) -> JsonObject:
        """List every agent the book knows, by name."""
        del request
        return {"agents": [agent_json(state) for state in agent_states(engine)]}

    del list_agents
