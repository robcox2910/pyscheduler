"""Test that dates at the edge of the calendar give the 400 HTML page, never a 500."""

import pytest
from api_helpers import MakeApi
from ui_helpers import find

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
