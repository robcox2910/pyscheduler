"""Test ``GET /api/runs/<id>/log`` for attempts that ran on an agent."""

from api_helpers import (
    ADHOC_RUN,
    AGENT_LOG,
    DEV,
    NOT_FOUND,
    REMOTE_AGENT,
    MakeApi,
    body_of,
    remote_attempt,
)

OK = 200
UNAVAILABLE = 503
TAIL = 1
LOG = f"/api/runs/{ADHOC_RUN}/log"


class TestRemoteLog:
    """The log route reads a remote attempt's log from its agent."""

    def test_tail_comes_from_the_agent(self, api: MakeApi) -> None:
        """Return the agent's lines, and its path as ``name:path`` (P4-R5)."""
        h = api(tokens=False)
        hub = remote_attempt(h, ended=True)

        async def body() -> None:
            """Ask for all the lines, then one."""
            both = await h.client.get(LOG, headers=DEV)
            assert both.status == OK
            assert body_of(both) == {
                "lines": ["one", "two"],
                "path": f"{REMOTE_AGENT}:{AGENT_LOG}",
                "truncated": False,
            }
            last = await h.client.get(f"{LOG}?tail={TAIL}", headers=DEV)
            assert body_of(last)["lines"] == ["two"]
            assert hub.log_requests == len(("both", "last"))

        h.run(body)

    def test_unknown_log_is_not_found(self, api: MakeApi) -> None:
        """An agent without the log, and an attempt not yet started, answer ``no log yet``."""
        h = api(tokens=False)
        hub = remote_attempt(h, ended=False)
        del hub.logs[ADHOC_RUN, 1, "stdout"]

        async def body() -> None:
            """Ask the agent that has no such log."""
            missing = await h.client.get(LOG, headers=DEV)
            assert (missing.status, body_of(missing)) == (NOT_FOUND, {"error": "no log yet"})

        h.run(body)

    def test_not_started_yet(self, api: MakeApi) -> None:
        """No agent-side path yet reads as ``no log yet``, without asking the agent."""
        h = api(tokens=False)
        hub = remote_attempt(h, ended=False, paths=False)

        async def body() -> None:
            """Ask before ``started`` has arrived."""
            early = await h.client.get(LOG, headers=DEV)
            assert (early.status, body_of(early)) == (NOT_FOUND, {"error": "no log yet"})
            assert hub.log_requests == 0

        h.run(body)

    def test_offline_agent(self, api: MakeApi) -> None:
        """An agent that is offline gives 503 and the reason."""
        h = api(tokens=False)
        hub = remote_attempt(h, ended=True)
        hub.disconnect(REMOTE_AGENT)

        async def body() -> None:
            """Let the agent go quiet past its grace, then ask."""
            await h.advance_to(h.clock.now() + 2 * hub.grace)
            gone = await h.client.get(LOG, headers=DEV)
            assert (gone.status, body_of(gone)) == (UNAVAILABLE, {"error": "the agent is offline"})

        h.run(body)

    def test_silent_agent(self, api: MakeApi) -> None:
        """An agent that does not answer in time gives 503 too."""
        h = api(tokens=False)
        hub = remote_attempt(h, ended=True)
        hub.silent.add(REMOTE_AGENT)

        async def body() -> None:
            """Ask an agent that never replies."""
            quiet = await h.client.get(LOG, headers=DEV)
            assert (quiet.status, body_of(quiet)) == (
                UNAVAILABLE,
                {"error": "the agent did not answer"},
            )

        h.run(body)
