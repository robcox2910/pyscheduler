"""The agents page at ``/agents``: who is online, how busy, and what each one runs.

This is the **courier firm's sign-in sheet on the office wall**. One row
per agent: a badge saying whether it is in (online) or gone (offline),
the skills it has (its tags), how full its hands are (``running/max``),
when it last called in (in the scheduler's time zone), and a link to every
attempt it holds right now.

The page keeps itself up to date: app.js listens for ``agent`` events on
the live feed and fetches that agent's row again from
``/ui/fragments/agent-row/<name>``, so the HTML is drawn (and escaped) in
one place only. Agent names and tags already follow the job-name rule;
they are escaped all the same. No secret is ever shown.
"""

from urllib.parse import quote
from zoneinfo import ZoneInfo

from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse, html

from pyscheduler.agents.book import AgentState
from pyscheduler.api.agents import agent_states
from pyscheduler.engine.core import Engine
from pyscheduler.ui.detail_view import stamp
from pyscheduler.ui.html import Safe, h, join
from pyscheduler.ui.layout import page, secure

__all__ = ["AGENT_ROW", "add_agents_page", "agent_row"]

AGENT_ROW = "/ui/fragments/agent-row/<name>"
"""Where app.js fetches one agent's row again."""

NOT_SET_UP = "Agents are not set up: the config has no agents: section with listen or broker."
NO_AGENTS = "No agent has reported yet."
_HEADS = ("Agent", "Status", "Tags", "Load", "Last heartbeat", "Running")
_NOT_FOUND = 404


def agent_row(state: AgentState, zone: ZoneInfo) -> Safe:
    """Return the ``<tr>`` for one agent (its last heartbeat in ``zone``)."""
    status = "online" if state.online else "offline"
    return h(
        "tr",
        {"data-agent": state.name},
        h("td", None, h("strong", None, state.name)),
        h("td", None, h("span", {"class": f"badge {status}"}, status)),
        h("td", None, *(h("span", {"class": "tag"}, tag) for tag in sorted(state.tags))),
        h("td", None, f"{len(state.running)}/{state.max_running}"),
        h("td", None, stamp(zone, state.last_seen)),
        h(
            "td",
            None,
            *(
                h("a", {"href": f"/runs/{quote(run_id, safe='')}"}, f"{run_id} #{attempt}")
                for run_id, attempt in sorted(state.running)
            ),
        ),
    )


def _table(states: list[AgentState], zone: ZoneInfo) -> Safe:
    """Return the agents table (kept even when empty, so live rows have a home)."""
    return join(
        h(
            "table",
            {"id": "agents", "class": "agents"},
            h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, x) for x in _HEADS))),
            h("tbody", None, *(agent_row(state, zone) for state in states)),
        ),
        h("p", {"id": "agents-empty", "class": "empty", "hidden": bool(states)}, NO_AGENTS),
    )


def _fragment(text: Safe, status: int = 200) -> HttpResponse:
    """Return a bit of HTML (not a page) with the security headers."""
    return secure(html(text, status))


def add_agents_page(app: App, engine: Engine) -> None:
    """Add ``/agents`` and its row fragment to ``app`` (the engine's placement knows the agents)."""

    @app.get("/agents")
    async def agents_view(request: HttpRequest) -> HttpResponse:
        """List every agent the book knows, by name."""
        zone = engine.config.settings.timezone
        if engine.placement is None:
            body = join(h("h1", None, "Agents"), h("p", {"class": "empty"}, NOT_SET_UP))
        else:
            body = join(h("h1", None, "Agents"), _table(agent_states(engine), zone))
        return page(request, title="Agents", body=body, active="agents")

    @app.get(AGENT_ROW)
    async def agent_fragment(request: HttpRequest, name: str) -> HttpResponse:
        """Draw one agent's row; 404 if the book does not know it."""
        del request
        states = [s for s in agent_states(engine) if s.name == name]
        if not states:
            return _fragment(h("p", None, f"no agent named {name}"), _NOT_FOUND)
        return _fragment(agent_row(states[0], engine.config.settings.timezone))

    del agents_view, agent_fragment
