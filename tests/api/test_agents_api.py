"""Test ``GET /api/agents`` and how a reload treats the ``agents:`` section and its secrets."""

from pathlib import Path

import pytest
from api_helpers import JOBS, OK, UNAUTHORISED, VIEWER, ApiHarness, MakeApi, body_of

from pyscheduler.api import config_actions
from pyscheduler.config.agent_secrets import AgentSecrets

AGENT = "build-01"
OTHER = "gpu-02"
SECRET = "s" * 40
SHORT_SECRET = "x" * 5
MAX_RUNNING = 2
RUN = "extract.2026-10-19.1"
SECRETS_MODE = 0o600
LISTEN = 'agents: {listen: "127.0.0.1:8471", secrets_file: agents.yaml}\n'
MOVED = 'agents: {listen: "127.0.0.1:8472", secrets_file: agents.yaml}\n'
AGENT_JOB = f"""
  remote:
    command: run-remote
    schedule: manual
    agent: {AGENT}
"""


def _secrets(tmp_path: Path, *names: str, secret: str = SECRET) -> None:
    """Write the agents' secrets file (owner-only) naming ``names``."""
    path = tmp_path / "agents.yaml"
    path.write_text("".join(f"- {{name: {n}, secret: {secret}}}\n" for n in names), "utf-8")
    path.chmod(SECRETS_MODE)


def _agents_config(api: MakeApi, tmp_path: Path) -> ApiHarness:
    """Return a harness whose config has an ``agents:`` section and one agent job."""
    _secrets(tmp_path, AGENT)
    return api(LISTEN + JOBS + AGENT_JOB, hub=True)


def _rewrite(h_: ApiHarness, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in the harness's config file."""
    text = h_.config_path.read_text("utf-8")
    h_.config_path.write_text(text.replace(old, new), "utf-8")


class TestAgentsRoute:
    """``GET /api/agents`` lists the book's agents for any caller with a token."""

    def test_lists_agents(self, api: MakeApi) -> None:
        """An online agent shows its tags, capacity, running attempts and last report."""
        h_ = api(hub=True)
        hub = h_.hub
        assert hub is not None

        async def body() -> None:
            """Bring an agent online, hold a slot on it and ask the API."""
            hub.connect(AGENT, tags=("linux", "gpu"), max_running=MAX_RUNNING)
            hub.reserve(AGENT, (RUN, 1))
            response = await h_.client.get("/api/agents", headers=VIEWER)
            assert response.status == OK
            assert body_of(response) == {
                "agents": [
                    {
                        "name": AGENT,
                        "online": True,
                        "tags": ["gpu", "linux"],
                        "max_running": MAX_RUNNING,
                        "running": [{"run_id": RUN, "attempt": 1}],
                        "last_seen": "2026-10-19T00:00:00Z",
                    }
                ]
            }

        h_.run(body)

    def test_no_hub_lists_none_and_needs_a_token(self, api: MakeApi) -> None:
        """Without agents the list is empty; without a token the answer is 401."""
        h_ = api()

        async def body() -> None:
            """Ask with and without a token."""
            assert body_of(await h_.client.get("/api/agents", headers=VIEWER)) == {"agents": []}
            assert (await h_.client.get("/api/agents")).status == UNAUTHORISED

        h_.run(body)


class TestReloadAgents:
    """A reload swaps the agents' secrets, and refuses to move where agents meet."""

    def test_reload_swaps_secrets(self, api: MakeApi, tmp_path: Path) -> None:
        """Editing only the secrets file is picked up by a reload (all of it at once)."""
        h_ = _agents_config(api, tmp_path)
        swapped: list[AgentSecrets] = []
        reload, _ = config_actions(h_.engine, h_.config_path, env={}, agent_secrets=swapped.append)

        async def body() -> None:
            """Add an agent to the secrets file and reload."""
            _secrets(tmp_path, AGENT, OTHER)
            assert [p for p in await reload("ops") if p.severity == "error"] == []
            assert [s.names for s in swapped] == [frozenset({AGENT, OTHER})]

        h_.run(body)

    def test_bad_secrets_file_fails_the_reload(self, api: MakeApi, tmp_path: Path) -> None:
        """A secrets file with a short secret fails the whole reload; nothing is swapped."""
        h_ = _agents_config(api, tmp_path)
        swapped: list[AgentSecrets] = []
        reload, _ = config_actions(h_.engine, h_.config_path, env={}, agent_secrets=swapped.append)

        async def body() -> None:
            """Break the secrets file and reload."""
            _secrets(tmp_path, AGENT, secret=SHORT_SECRET)
            problems = await reload("ops")
            assert any(p.severity == "error" for p in problems)
            assert all(SECRET not in p.message and SHORT_SECRET not in p.message for p in problems)
            assert swapped == []

        h_.run(body)

    def test_moving_the_broker_needs_a_restart(self, api: MakeApi, tmp_path: Path) -> None:
        """A new listen address fails the reload with a problem; the running config stays."""
        h_ = _agents_config(api, tmp_path)
        swapped: list[AgentSecrets] = []
        reload, _ = config_actions(h_.engine, h_.config_path, env={}, agent_secrets=swapped.append)
        before = h_.engine.config

        async def body() -> None:
            """Move the listen port and reload."""
            _rewrite(h_, LISTEN, MOVED)
            [problem] = await reload("ops")
            assert "needs a restart" in problem.message
            assert h_.engine.config is before
            assert swapped == []

        h_.run(body)

    @pytest.mark.parametrize(("field", "value"), [("agent_grace", "30s"), ("lost_after", "5m")])
    def test_changing_a_timing_needs_a_restart(
        self, api: MakeApi, tmp_path: Path, field: str, value: str
    ) -> None:
        """A new ``agent_grace`` or ``lost_after`` fails the reload, naming it; nothing changes."""
        h_ = _agents_config(api, tmp_path)
        swapped: list[AgentSecrets] = []
        reload, _ = config_actions(h_.engine, h_.config_path, env={}, agent_secrets=swapped.append)
        before = h_.engine.config

        async def body() -> None:
            """Change the timing and reload."""
            _rewrite(
                h_, "secrets_file: agents.yaml}", f"secrets_file: agents.yaml, {field}: {value}}}"
            )
            [problem] = await reload("ops")
            assert f"agents: changing {field} needs a restart" in problem.message
            assert h_.engine.config is before
            assert swapped == []

        h_.run(body)

    def test_turning_agents_on_needs_a_restart(self, api: MakeApi, tmp_path: Path) -> None:
        """Adding an ``agents:`` section to a running scheduler fails the reload."""
        h_ = api()
        reload, _ = config_actions(h_.engine, h_.config_path, env={})

        async def body() -> None:
            """Add the section and reload."""
            _secrets(tmp_path, AGENT)
            text = h_.config_path.read_text("utf-8")
            h_.config_path.write_text(LISTEN + text, "utf-8")
            [problem] = await reload("ops")
            assert "needs a restart" in problem.message
            assert h_.engine.config.agents is None

        h_.run(body)
