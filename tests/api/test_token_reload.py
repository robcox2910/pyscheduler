"""Test that a reload re-reads the tokens file (ruling R33)."""

from api_helpers import (
    BAD_REQUEST,
    OK,
    OPERATOR,
    UNAUTHORISED,
    VIEWER,
    MakeApi,
    body_of,
    sha,
)

NEW_KEY = "new-operator-secret"
NEW_OPERATOR = {"Authorization": f"Bearer {NEW_KEY}", "Content-Type": "application/json"}


class TestTokenReload:
    """``POST /api/reload`` swaps in the tokens file as it is now."""

    def test_revoked_token_stops_working(self, api: MakeApi) -> None:
        """A token removed from the file is refused right after the reload."""
        h = api()

        async def body() -> None:
            """Replace the operator, reload, and try both tokens."""
            tokens = h.tmp_path / "tokens.yaml"
            tokens.write_text(
                f"- {{name: nina, token_sha256: {sha(NEW_KEY)}, role: operator}}\n",
                encoding="utf-8",
            )
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == OK, response.text
            assert (await h.client.get("/api/jobs", headers=OPERATOR)).status == UNAUTHORISED
            assert (await h.client.get("/api/jobs", headers=VIEWER)).status == UNAUTHORISED
            assert (await h.client.get("/api/jobs", headers=NEW_OPERATOR)).status == OK

        h.run(body)

    def test_bad_tokens_file_fails_the_reload(self, api: MakeApi) -> None:
        """A broken tokens file fails the whole reload; config and tokens stay."""
        h = api()

        async def body() -> None:
            """Break the tokens file, add a job, and reload."""
            (h.tmp_path / "tokens.yaml").write_text("- just-text\n", encoding="utf-8")
            text = h.config_path.read_text(encoding="utf-8")
            h.config_path.write_text(
                text + "  extra:\n    command: run-extra\n    schedule: manual\n",
                encoding="utf-8",
            )
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == BAD_REQUEST
            messages = [p["message"] for p in body_of(response)["problems"]]
            assert any("api_tokens_file" in m and "mapping" in m for m in messages)
            assert "extra" not in h.engine.config.jobs
            assert (await h.client.get("/api/jobs", headers=VIEWER)).status == OK

        h.run(body)

    def test_mode_switch_needs_a_restart(self, api: MakeApi) -> None:
        """Removing ``api_tokens_file`` cannot turn on development mode by a reload."""
        h = api()

        async def body() -> None:
            """Drop the tokens line and reload."""
            text = h.config_path.read_text(encoding="utf-8")
            kept = [line for line in text.splitlines() if "api_tokens_file" not in line]
            h.config_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == BAD_REQUEST
            messages = [p["message"] for p in body_of(response)["problems"]]
            assert any("restart" in m for m in messages)
            assert (await h.client.get("/api/jobs", headers=VIEWER)).status == OK

        h.run(body)
