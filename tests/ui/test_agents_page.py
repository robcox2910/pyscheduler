"""Test the agents page (``/agents``), its row fragment, its live news and agents on run pages."""

from typing import Any
from urllib.parse import quote

from api_helpers import (
    ADHOC_RUN,
    REMOTE_AGENT,
    START,
    ApiHarness,
    MakeApi,
    remote_attempt,
)
from pynet.messaging import JsonValue
from ui_helpers import find

from pyscheduler.agents.book import AgentBook
from pyscheduler.engine.clock import FakeClock
from pyscheduler.fake_hub import AGENT_GRACE
from pyscheduler.ui.layout import NAV
from pyscheduler.ui.live import LiveFeed

OK = 200
NOT_FOUND = 404
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
AGENT = "build-01"
QUIET = "gpu-02"
MAX_RUNNING = 2
RUN = "extract.2026-10-19.1"
ROW = "/ui/fragments/agent-row/"


def _get(h_: ApiHarness, path: str) -> Any:
    """GET ``path`` in development mode, asking for HTML."""
    return h_.client.get(path, headers=DEV_HTML)


def _rows(text: str) -> dict[str, str]:
    """Return each agent row's text by the agent's name."""
    return {
        attrs["data-agent"] or "": inner
        for _, attrs, inner in find(text, "tr")
        if "data-agent" in attrs
    }


class TestAgentsPage:
    """The page shows each agent's status, tags, load, last report and running attempts."""

    def test_lists_agents(self, api: MakeApi) -> None:
        """One row per agent: badge, tags, load, local last heartbeat and attempt links."""
        h_ = api(tokens=False, hub=True)
        hub = h_.hub
        assert hub is not None

        async def body() -> None:
            """Bring one agent online with a slot held, let another go quiet, draw the page."""
            hub.connect(AGENT, tags=("linux",), max_running=MAX_RUNNING)
            hub.connect(QUIET, max_running=MAX_RUNNING)
            hub.disconnect(QUIET)
            hub.reserve(AGENT, (RUN, 1))
            await h_.advance_to(START + AGENT_GRACE * 2)
            response = await _get(h_, "/agents")
            assert response.status == OK
            rows = _rows(response.text)
            assert sorted(rows) == [AGENT, QUIET]
            assert "online" in rows[AGENT]
            assert "offline" in rows[QUIET]
            assert "linux" in rows[AGENT]
            assert f"1/{MAX_RUNNING}" in rows[AGENT]
            assert f"0/{MAX_RUNNING}" in rows[QUIET]
            [link] = [a for a in find(response.text, "a") if a[1].get("href") == f"/runs/{RUN}"]
            assert RUN in link[2]
            stamps = [t[1]["datetime"] for t in find(response.text, "time")]
            assert all(stamp and stamp.endswith("Z") for stamp in stamps)
            assert len(stamps) == len(rows)

        h_.run(body)

    def test_row_fragment(self, api: MakeApi) -> None:
        """The fragment draws one agent's row; an unknown agent is 404."""
        h_ = api(tokens=False, hub=True)
        hub = h_.hub
        assert hub is not None

        async def body() -> None:
            """Fetch a known and an unknown agent's row."""
            hub.connect(AGENT, max_running=MAX_RUNNING)
            found = await _get(h_, ROW + AGENT)
            assert found.status == OK
            assert list(_rows(found.text)) == [AGENT]
            assert "<html" not in found.text
            assert (await _get(h_, ROW + "ghost")).status == NOT_FOUND

        h_.run(body)

    def test_without_agents(self, api: MakeApi) -> None:
        """With no hub the page says agents are not set up, and the fragment is 404."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Draw the page and a fragment."""
            response = await _get(h_, "/agents")
            assert response.status == OK
            assert "not set up" in response.text
            assert (await _get(h_, ROW + AGENT)).status == NOT_FOUND

        h_.run(body)

    def test_nav_entry(self) -> None:
        """Viewers see the Agents menu entry."""
        assert ("agents", "/agents", "Agents", "viewer") in NAV


class _Recorder(LiveFeed):
    """A live feed that also keeps what it published."""

    def __init__(self) -> None:
        """Start with nothing published."""
        super().__init__(boot="boot")
        self.seen: list[tuple[str, dict[str, JsonValue]]] = []

    def publish(self, kind: str, data: Any) -> str:
        """Keep the event, then publish it."""
        self.seen.append((kind, dict(data)))
        return super().publish(kind, data)


class TestAgentNews:
    """``attach_agents`` publishes an ``agent`` event whenever the book changes."""

    def test_publishes_agent_events(self) -> None:
        """Each change becomes ``{name, online, running}``; detaching stops the news."""
        clock = FakeClock(START)
        book = AgentBook(clock=clock, grace=AGENT_GRACE)
        feed = _Recorder()
        detach = feed.attach_agents(book)
        book.heard(AGENT, max_running=MAX_RUNNING)
        book.reserve(AGENT, (RUN, 1))
        detach()
        book.release(AGENT, (RUN, 1))
        assert feed.seen == [
            ("agent", {"name": AGENT, "online": True, "running": 0}),
            ("agent", {"name": AGENT, "online": True, "running": 1}),
        ]


class TestRemoteRunsShowTheirAgent:
    """The board and the run page name the agent a remote attempt runs on."""

    def test_board_and_run_page(self, api: MakeApi) -> None:
        """The board row and the attempts table both show the agent's name."""
        h_ = api(tokens=False)
        remote_attempt(h_, ended=False)

        async def body() -> None:
            """Draw the board and the run page."""
            board = (await _get(h_, "/")).text
            [row] = [r for r in find(board, "tr") if r[1].get("data-run-id") == ADHOC_RUN]
            assert REMOTE_AGENT in row[2]
            run_page = (await _get(h_, f"/runs/{quote(ADHOC_RUN, safe='')}")).text
            [attempt] = find(run_page, "tr", **{"data-attempt": "1"})
            assert REMOTE_AGENT in attempt[2]

        h_.run(body)
