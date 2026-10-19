"""Test that dates at the edge of the calendar give the 400 HTML page, never a 500."""

import pytest
from api_helpers import MakeApi
from ui_helpers import find

OK = 200
BAD_REQUEST = 400
SERVER_ERROR = 500
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
EXTREMES = ("9999-12-31", "0001-01-01", "9999-12-30", "0001-01-02")
PAGE_QUERY = {
    "/": "date={day}",
    "/graph": "run_date={day}",
    "/forecast": "from={day}T00:00:00Z",
}
"""The query that puts each page at an extreme day."""


class TestExtremeDates:
    """A well-formed date the scheduler cannot handle is refused with a 400 page."""

    @pytest.mark.parametrize("day", EXTREMES)
    @pytest.mark.parametrize("zoom", ["hour", "day", "week"])
    def test_timeline(self, api: MakeApi, day: str, zoom: str) -> None:
        """The timeline answers 400 with the error shown in the shell."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Ask for the extreme day."""
            response = await h_.client.get(f"/timeline?zoom={zoom}&date={day}", headers=DEV_HTML)
            assert response.status == BAD_REQUEST
            assert find(response.text, "p", role="alert")

        h_.run(body)

    @pytest.mark.parametrize("path", sorted(PAGE_QUERY))
    @pytest.mark.parametrize("day", EXTREMES)
    def test_other_pages(self, api: MakeApi, path: str, day: str) -> None:
        """The board, graph and forecast never answer 500 for an extreme date."""
        h_ = api(tokens=False)
        query = PAGE_QUERY[path].format(day=day)

        async def body() -> None:
            """Ask for the extreme day."""
            response = await h_.client.get(f"{path}?{query}", headers=DEV_HTML)
            assert response.status < SERVER_ERROR, response.text[-300:]

        h_.run(body)


FIRST = "1900-01-01"
LAST = "2999-12-31"
LINKS = {
    "/": ("date={day}", "date"),
    "/graph": ("run_date={day}", "run_date"),
}
"""The query that puts the board and the graph on a day, as written in their links."""


class TestBoundLinks:
    """The previous/next links are left out where they would lead to the 400 page."""

    @pytest.mark.parametrize("path", sorted(LINKS))
    def test_board_and_graph(self, api: MakeApi, path: str) -> None:
        """No previous link on the first day, no next link on the last, both between."""
        h_ = api(tokens=False)
        template = LINKS[path][0]

        async def body() -> None:
            """Open the first, the last and a middle day."""
            first = await h_.client.get(f"{path}?{template.format(day=FIRST)}", headers=DEV_HTML)
            assert first.status == OK
            assert not find(first.text, "a", rel="prev")
            assert find(first.text, "a", rel="next")
            last = await h_.client.get(f"{path}?{template.format(day=LAST)}", headers=DEV_HTML)
            assert last.status == OK
            assert not find(last.text, "a", rel="next")
            assert find(last.text, "a", rel="prev")
            mid = await h_.client.get(
                f"{path}?{template.format(day='2026-10-19')}", headers=DEV_HTML
            )
            assert find(mid.text, "a", rel="prev")
            assert find(mid.text, "a", rel="next")

        h_.run(body)

    @pytest.mark.parametrize(
        ("zoom", "day", "gone", "kept"),
        [
            ("day", FIRST, "prev", "next"),
            ("day", LAST, "next", "prev"),
            ("week", "1900-01-05", "prev", "next"),
            ("week", "2999-12-28", "next", "prev"),
            ("week", "1900-01-08", None, "prev"),
        ],
    )
    def test_timeline(self, api: MakeApi, zoom: str, day: str, gone: str | None, kept: str) -> None:
        """A step that would leave 1900-2999 gets no link, and every offered link answers 200."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Open the timeline and follow every previous/next link."""
            text = (await h_.client.get(f"/timeline?zoom={zoom}&date={day}", headers=DEV_HTML)).text
            if gone:
                assert not find(text, "a", rel=gone)
            assert find(text, "a", rel=kept)
            for rel in ("prev", "next"):
                for link in find(text, "a", rel=rel):
                    page = await h_.client.get(link[1]["href"] or "", headers=DEV_HTML)
                    assert page.status == OK, link

        h_.run(body)
