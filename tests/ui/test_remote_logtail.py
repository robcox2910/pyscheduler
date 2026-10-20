"""Test the live log tail of attempts that ran on an agent."""

import json

import pytest
from api_helpers import ADHOC_RUN, DEV, REMOTE_AGENT, MakeApi, remote_attempt
from ui_helpers import next_event, open_stream

from pyscheduler.ui import logtail

QUICK_POLL = 0.01
"""A test's poll interval (real seconds), instead of ``LOG_POLL``."""
TAIL = f"/events/log/{ADHOC_RUN}"
NOT_FOUND = 404


class TestRemoteLogTail:
    """The tail asks the agent for the log instead of opening a file."""

    def test_streams_remote_lines_and_ends_on_finished(
        self, api: MakeApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Send the agent's lines, then end once the attempt is over."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)
        hub = remote_attempt(h, ended=True)

        async def body() -> None:
            """Tail a finished remote attempt."""
            events = await h.client.stream(f"{TAIL}?stream=stdout", count=10, headers=DEV)
            assert [(e.event, e.data) for e in events] == [
                ("lines", json.dumps(["one", "two"])),
                ("end", "{}"),
            ]
            assert hub.log_requests > 0

        h.run(body)

    def test_follows_new_remote_lines(self, api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lines the agent writes later arrive on a later poll."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)
        hub = remote_attempt(h, ended=False)

        async def body() -> None:
            """Open the tail, then let the agent's log grow."""
            stream = await open_stream(h, TAIL, DEV)
            assert json.loads((await next_event(stream)).data) == ["one", "two"]
            hub.logs[ADHOC_RUN, 1, "stdout"] += b"three\nfour"
            event = await next_event(stream)
            assert (event.event, json.loads(event.data)) == ("lines", ["three"])
            await stream.aclose()

        h.run(body)

    def test_offline_agent_ends_the_tail(
        self, api: MakeApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent that is offline ends the tail with the message, at once or mid-tail."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)
        hub = remote_attempt(h, ended=False)

        async def body() -> None:
            """Go offline while tailing, then try to start a tail on the offline agent."""
            stream = await open_stream(h, TAIL, DEV)
            assert json.loads((await next_event(stream)).data) == ["one", "two"]
            hub.disconnect(REMOTE_AGENT)
            await h.advance_to(h.clock.now() + 2 * hub.grace)
            gone = await next_event(stream)
            assert (gone.event, json.loads(gone.data)) == (
                "end",
                {"message": "the agent is offline"},
            )
            with pytest.raises(StopAsyncIteration):
                await next_event(stream)
            events = await h.client.stream(TAIL, count=10, headers=DEV)
            assert [(e.event, json.loads(e.data)) for e in events] == [
                ("end", {"message": "the agent is offline"})
            ]

        h.run(body)

    def test_silent_agent_ends_the_tail(self, api: MakeApi) -> None:
        """An agent that does not answer ends the tail saying so."""
        h = api(tokens=False)
        hub = remote_attempt(h, ended=False)
        hub.silent.add(REMOTE_AGENT)

        async def body() -> None:
            """Tail an agent that never replies."""
            events = await h.client.stream(TAIL, count=10, headers=DEV)
            assert [(e.event, json.loads(e.data)) for e in events] == [
                ("end", {"message": "the agent did not answer"})
            ]

        h.run(body)

    def test_not_started_yet(self, api: MakeApi) -> None:
        """An attempt with no agent-side path yet answers ``no log yet`` like a local one."""
        h = api(tokens=False)
        remote_attempt(h, ended=False, paths=False)

        async def body() -> None:
            """Tail before ``started`` has arrived."""
            response = await h.client.get(TAIL, headers=DEV)
            assert response.status == NOT_FOUND

        h.run(body)
