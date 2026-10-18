"""Test the guards against cross-site posts and forged hosts (ruling R24)."""

import pytest
from api_helpers import FORBIDDEN, OK, OPERATOR, TODAY, MakeApi, body_of

UNSUPPORTED_MEDIA = 415
LOOPBACK = {"Host": "127.0.0.1:8470"}
JSON_TYPE = {"Content-Type": "application/json"}


class TestContentType:
    """Every POST must say its body is JSON, even an empty one."""

    @pytest.mark.parametrize("tokens", [True, False])
    def test_text_plain_post_is_refused(self, api: MakeApi, *, tokens: bool) -> None:
        """Refuse a ``text/plain`` POST (what a cross-site form can send) with 415."""
        h = api(tokens=tokens)
        auth = OPERATOR if tokens else {}

        async def body() -> None:
            """Post the way a browser on another site could."""
            headers = {**auth, **LOOPBACK, "Content-Type": "text/plain"}
            response = await h.client.post("/api/jobs/adhoc/trigger", body=b"", headers=headers)
            assert response.status == UNSUPPORTED_MEDIA
            assert body_of(response) == {"error": "use Content-Type: application/json"}
            assert h.engine.book.runs_for("adhoc", TODAY) == []

        h.run(body)

    def test_missing_content_type_is_refused(self, api: MakeApi) -> None:
        """Refuse a body-less POST with no Content-Type at all."""
        h = api(tokens=False)

        async def body() -> None:
            """Post nothing, with no type."""
            response = await h.client.post("/api/reload", headers=LOOPBACK)
            assert response.status == UNSUPPORTED_MEDIA

        h.run(body)

    def test_json_with_charset_is_accepted(self, api: MakeApi) -> None:
        """Accept ``application/json; charset=utf-8`` with an empty body."""
        h = api(tokens=False)

        async def body() -> None:
            """Post an empty JSON request."""
            headers = {**LOOPBACK, "Content-Type": "Application/JSON; charset=utf-8"}
            response = await h.client.post("/api/jobs/adhoc/trigger", headers=headers)
            assert response.status == OK

        h.run(body)


class TestDevModeHost:
    """In development mode only a loopback ``Host`` is answered."""

    @pytest.mark.parametrize(
        "host", ["evil.example", "evil.example:8470", "10.0.0.1", "localhost.evil.example"]
    )
    def test_forged_host_is_refused(self, api: MakeApi, host: str) -> None:
        """Refuse a request whose ``Host`` is not this machine with 403."""
        h = api(tokens=False)

        async def body() -> None:
            """Ask for the jobs and post a trigger under a foreign host name."""
            got = await h.client.get("/api/jobs", headers={"Host": host})
            assert got.status == FORBIDDEN
            assert body_of(got) == {"error": "forbidden host"}
            headers = {"Host": host, **JSON_TYPE}
            posted = await h.client.post("/api/jobs/adhoc/trigger", headers=headers)
            assert posted.status == FORBIDDEN
            assert h.engine.book.runs_for("adhoc", TODAY) == []

        h.run(body)

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.1:8470",
            "127.9.9.9:1",
            "localhost",
            "LOCALHOST:80",
            "[::1]:8470",
            "[::1]",
        ],
    )
    def test_loopback_host_is_answered(self, api: MakeApi, host: str) -> None:
        """Answer requests addressed to a loopback name or address."""
        h = api(tokens=False)

        async def body() -> None:
            """Read the jobs under a loopback host."""
            assert (await h.client.get("/api/jobs", headers={"Host": host})).status == OK

        h.run(body)

    def test_any_host_with_tokens(self, api: MakeApi) -> None:
        """With tokens, the token is the guard: any ``Host`` is answered."""
        h = api()

        async def body() -> None:
            """Read the jobs under a public host name."""
            headers = {**OPERATOR, "Host": "scheduler.example.com"}
            assert (await h.client.get("/api/jobs", headers=headers)).status == OK

        h.run(body)
