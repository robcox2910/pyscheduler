"""Test the forecast page (``/forecast``): rows match the API, local times, bad windows."""

from typing import Any

import pytest
from api_helpers import ApiHarness, MakeApi, body_of
from ui_helpers import find

OK = 200
BAD_REQUEST = 400
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
DEV_JSON = {"Host": "127.0.0.1:8470"}
COLUMNS = 5


def _get(h_: ApiHarness, path: str) -> Any:
    """GET ``path`` in development mode, asking for HTML."""
    return h_.client.get(path, headers=DEV_HTML)


class TestForecastPage:
    """The page lists what the API forecast lists."""

    def test_forecast_page(self, api: MakeApi) -> None:
        """Rows match the API's entries: job, run date, via, and the expected start."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Fetch the page and the API for the same default window."""
            response = await _get(h_, "/forecast")
            assert response.status == OK
            entries = body_of(await h_.client.get("/api/forecast", headers=DEV_JSON))["entries"]
            rows = find(response.text, "tr")
            jobs = [r[1]["data-job"] for r in rows if "data-job" in r[1]]
            assert jobs == [e["job"] for e in entries] == ["extract", "load"]
            (extract_row, load_row) = (r for r in rows if "data-job" in r[1])
            assert "2026-10-19 18:30:00" in extract_row[2]
            assert "2026-10-19" in load_row[2]
            assert load_row[2].rstrip().endswith("extract")
            assert len(find(response.text, "th")) == COLUMNS
            stamps = [t[1]["datetime"] for t in find(response.text, "time")]
            assert stamps == [e["expected_start"] for e in entries]

        h_.run(body)

    def test_window_from_and_to(self, api: MakeApi) -> None:
        """A given window picks the entries inside it; an empty one says so."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Ask for tomorrow, then for the quiet morning."""
            tomorrow = {"from": "2026-10-20T00:00:00Z", "to": "2026-10-21T00:00:00Z"}
            text = (await h_.client.get("/forecast", query=tomorrow, headers=DEV_HTML)).text
            assert "2026-10-20 18:30:00" in text
            quiet = {"from": "2026-10-20T00:00:00Z", "to": "2026-10-20T01:00:00Z"}
            text = (await h_.client.get("/forecast", query=quiet, headers=DEV_HTML)).text
            assert "No runs are expected" in text

        h_.run(body)

    @pytest.mark.parametrize(
        "query",
        [
            {"from": "tomorrow"},
            {"from": "2026-10-20T00:00:00"},
            {"from": "2026-10-20T00:00:00Z", "to": "2026-10-19T00:00:00Z"},
            {"from": "2026-10-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
        ],
    )
    def test_bad_window_is_400_html(self, api: MakeApi, query: dict[str, str]) -> None:
        """A bad ``from``/``to`` gives a 400 HTML page, not JSON."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Fetch the bad window."""
            response = await h_.client.get("/forecast", query=query, headers=DEV_HTML)
            assert response.status == BAD_REQUEST
            assert "text/html" in (response.headers.get("Content-Type") or "")
            assert find(response.text, "p", role="alert")
            assert find(response.text, "nav")

        h_.run(body)
