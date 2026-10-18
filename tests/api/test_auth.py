"""Test the bearer-token table and the API's token middleware."""

from pathlib import Path

import pytest
from api_helpers import (
    DEV,
    EXTRACT_RUN,
    FORBIDDEN,
    OK,
    OPERATOR,
    OPERATOR_KEY,
    UNAUTHORISED,
    VIEWER,
    VIEWER_KEY,
    MakeApi,
    body_of,
    sha,
    tokens_yaml,
)

from pyscheduler.api import TokenFileError, TokenTable
from pyscheduler.engine.model import Event


def _write(tmp_path: Path, text: str) -> Path:
    """Write a tokens file and return its path."""
    path = tmp_path / "tokens.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _two(first: str, first_token: str, second: str, second_token: str) -> str:
    """Return a tokens file with two viewer entries."""
    return (
        f"- {{name: {first}, token_sha256: {sha(first_token)}, role: viewer}}\n"
        f"- {{name: {second}, token_sha256: {sha(second_token)}, role: viewer}}\n"
    )


class TestTokenTable:
    """Load a tokens file and look tokens up by their hash."""

    def test_lookup_known_and_unknown(self, tmp_path: Path) -> None:
        """Return the name and role of a known token, ``None`` for anything else."""
        table = TokenTable.load(_write(tmp_path, tokens_yaml()))
        assert table.lookup(VIEWER_KEY) == ("vera", "viewer")
        assert table.lookup(OPERATOR_KEY) == ("otto", "operator")
        assert table.lookup("nope") is None
        assert table.lookup("") is None

    def test_block_style_list(self, tmp_path: Path) -> None:
        """Accept the block (dash and indent) style too."""
        text = f"- name: ci\n  token_sha256: {sha('x')}\n  role: operator\n"
        assert TokenTable.load(_write(tmp_path, text)).lookup("x") == ("ci", "operator")

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("name: x\n", "a list"),
            ("- just-text\n", "a mapping"),
            (f"- {{name: a, token_sha256: {sha('x')}}}\n", "role"),
            (f"- {{name: a, token_sha256: {sha('x')}, role: admin}}\n", "viewer or operator"),
            ("- {name: a, token_sha256: abc, role: viewer}\n", "64 lowercase hex"),
            (f"- {{name: a, token_sha256: {sha('x').upper()}, role: viewer}}\n", "lowercase hex"),
            (f"- {{name: 'a b', token_sha256: {sha('x')}, role: viewer}}\n", "name"),
            (f"- {{name: a, token_sha256: {sha('x')}, role: viewer, x: 1}}\n", "unknown"),
            (_two("a", "x", "a", "y"), "twice"),
            (_two("a", "x", "b", "x"), "twice"),
            ("- [unclosed\n", "tokens.yaml"),
        ],
    )
    def test_bad_files_raise(self, tmp_path: Path, text: str, message: str) -> None:
        """Raise ``TokenFileError`` naming the file and the problem."""
        with pytest.raises(TokenFileError, match=message) as caught:
            TokenTable.load(_write(tmp_path, text))
        assert "tokens.yaml" in str(caught.value)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Raise ``TokenFileError`` for a file that cannot be read."""
        with pytest.raises(TokenFileError, match="cannot read"):
            TokenTable.load(tmp_path / "missing.yaml")


class TestMiddleware:
    """Check the token and the role on every request but the health check."""

    def test_health_no_auth_needed(self, api: MakeApi) -> None:
        """Answer the health check without a token."""
        h = api()

        async def body() -> None:
            """Ask for the health check with no Authorization header."""
            response = await h.client.get("/api/health")
            assert response.status == OK
            assert body_of(response) == {"status": "ok", "timezone": "UTC", "today": "2026-10-19"}

        h.run(body)

    def test_tokens(self, api: MakeApi) -> None:
        """Unknown or missing token 401, viewer POST 403, viewer GET and operator POST 200."""
        h = api()

        async def body() -> None:
            """Try each kind of caller."""
            missing = await h.client.get("/api/jobs")
            assert missing.status == UNAUTHORISED
            assert body_of(missing) == {"error": "unauthorised"}
            assert missing.headers.get("WWW-Authenticate") == "Bearer"
            unknown = await h.client.get("/api/jobs", headers={"Authorization": "Bearer nope"})
            assert unknown.status == UNAUTHORISED
            basic = await h.client.get("/api/jobs", headers={"Authorization": "Basic abc"})
            assert basic.status == UNAUTHORISED
            assert (await h.client.get("/api/jobs", headers=VIEWER)).status == OK
            refused = await h.client.post("/api/jobs/adhoc/trigger", headers=VIEWER)
            assert refused.status == FORBIDDEN
            assert body_of(refused) == {"error": "forbidden"}
            done = await h.client.post("/api/jobs/adhoc/trigger", headers=OPERATOR)
            assert done.status == OK

        h.run(body)

    def test_actor_is_the_token_name(self, api: MakeApi) -> None:
        """Record the operator's token name as the actor of the action's events."""
        h = api()
        events: list[Event] = []

        async def body() -> None:
            """Hold a run with the operator token and read its events back."""
            await h.client.post("/api/jobs/extract/trigger", headers=OPERATOR)
            await h.engine.settle()
            response = await h.client.get(f"/api/runs/{EXTRACT_RUN}", headers=VIEWER)
            assert response.status == OK
            actors = {event["actor"] for event in body_of(response)["events"]}
            assert "otto" in actors
            events.extend(h.store.events_for(EXTRACT_RUN))

        h.run(body)
        assert any(event.actor == "otto" for event in events)

    def test_dev_mode_without_tokens(self, api: MakeApi) -> None:
        """Without a tokens table every request acts as ``dev`` with the operator role."""
        h = api(tokens=False)

        async def body() -> None:
            """Read and act with no token at all."""
            assert (await h.client.get("/api/jobs", headers=DEV)).status == OK
            response = await h.client.post("/api/jobs/adhoc/trigger", headers=DEV)
            assert response.status == OK
            run_id = body_of(response)["run"]["run_id"]
            detail = body_of(await h.client.get(f"/api/runs/{run_id}", headers=DEV))
            assert any(event["actor"] == "dev" for event in detail["events"])

        h.run(body)
