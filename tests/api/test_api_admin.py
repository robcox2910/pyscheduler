"""Test reload, validate, the health check and the error handling of the API."""

import logging

import pytest
from api_helpers import (
    BAD_REQUEST,
    NOT_FOUND,
    OK,
    OPERATOR,
    VIEWER,
    MakeApi,
    body_of,
)

INTERNAL_ERROR = 500
UNAVAILABLE = 503
BROKEN = "scheduler:\n  max_running: lots\n"


class TestReload:
    """Reload the config files from disk through the API."""

    def test_reload_invalid_returns_problems(self, api: MakeApi) -> None:
        """Answer 400 with the problems; the old config stays in force."""
        h = api()

        async def body() -> None:
            """Break the file, then ask for a reload."""
            before = h.engine.config.digest
            h.config_path.write_text(BROKEN, encoding="utf-8")
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == BAD_REQUEST
            answer = body_of(response)
            assert answer["error"] == "config has problems"
            assert answer["problems"]
            problem = answer["problems"][0]
            assert set(problem) == {"file", "line", "message", "job", "severity"}
            assert h.engine.config.digest == before

        h.run(body)

    def test_reload_valid_swaps_config(self, api: MakeApi) -> None:
        """Load a changed, valid config and record the token name as the actor."""
        h = api()

        async def body() -> None:
            """Add a job to the file and reload."""
            text = h.config_path.read_text(encoding="utf-8")
            h.config_path.write_text(
                text + "  extra:\n    command: run-extra\n    schedule: manual\n",
                encoding="utf-8",
            )
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == OK, response.text
            assert body_of(response) == {"ok": True, "warnings": []}
            assert "extra" in h.engine.config.jobs
            assert h.engine.config_version == 1 + 1

        h.run(body)


class TestValidate:
    """Check the config files on disk without loading them."""

    def test_validate_endpoint(self, api: MakeApi) -> None:
        """Answer 200 for a good file and 400 with problems for a bad one."""
        h = api()

        async def body() -> None:
            """Validate, break the file, validate again."""
            good = await h.client.get("/api/config/validate", headers=VIEWER)
            assert good.status == OK
            assert body_of(good)["ok"] is True
            before = h.engine.config.digest
            h.config_path.write_text(BROKEN, encoding="utf-8")
            bad = await h.client.get("/api/config/validate", headers=VIEWER)
            assert bad.status == BAD_REQUEST
            assert body_of(bad)["problems"]
            assert h.engine.config.digest == before

        h.run(body)


class TestErrors:
    """Never leak internals; report a failed engine."""

    def test_unexpected_error_is_a_plain_500(
        self, api: MakeApi, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Answer 500 ``internal error`` and log the traceback."""
        h = api()

        def explode(**_: object) -> None:
            """Fail like a bug would."""
            msg = "secret internals"
            raise RuntimeError(msg)

        async def body() -> None:
            """Ask for the runs while the book is broken."""
            monkeypatch.setattr(h.engine.book, "published_runs", explode)
            with caplog.at_level(logging.ERROR):
                response = await h.client.get("/api/runs", headers=VIEWER)
            assert response.status == INTERNAL_ERROR
            assert body_of(response) == {"error": "internal error"}
            assert "secret internals" in caplog.text

        h.run(body)

    def test_unknown_route_is_404(self, api: MakeApi) -> None:
        """Answer 404 for a path that is not in the API."""
        h = api()

        async def body() -> None:
            """Ask for a made-up path."""
            response = await h.client.get("/api/nothing", headers=VIEWER)
            assert response.status == NOT_FOUND

        h.run(body)

    def test_health_reports_failed_engine(self, api: MakeApi) -> None:
        """Answer 503 when the engine has failed stop, without the cause."""
        h = api()

        async def body() -> None:
            """Mark the engine failed and ask for the health check."""
            h.engine.error = RuntimeError("disk full")
            response = await h.client.get("/api/health")
            assert response.status == UNAVAILABLE
            assert body_of(response) == {"status": "failed"}
            h.engine.error = None

        h.run(body)


class TestMinorChecks:
    """Refuse oversized numbers and windows; hide the cause of a failed engine."""

    def test_long_numbers_and_far_windows_are_400(self, api: MakeApi) -> None:
        """Refuse a limit with too many digits and a forecast past year 9999."""
        h = api()

        async def body() -> None:
            """Ask with the extreme values."""
            huge = await h.client.get("/api/runs", query={"limit": "9" * 5000}, headers=VIEWER)
            assert huge.status == BAD_REQUEST
            padded = await h.client.get("/api/runs", query={"limit": "00001"}, headers=VIEWER)
            assert padded.status == BAD_REQUEST
            far = {"from": "9999-12-31T12:00:00+00:00"}
            response = await h.client.get("/api/forecast", query=far, headers=VIEWER)
            assert response.status == BAD_REQUEST
            assert "outside" in body_of(response)["error"]

        h.run(body)

    def test_failed_engine_message_is_fixed(self, api: MakeApi) -> None:
        """Answer 503 with a fixed message, not the failure's details."""
        h = api()

        async def body() -> None:
            """Mark the engine failed and try an action."""
            h.engine.error = RuntimeError("disk /secret/path full")
            response = await h.client.post("/api/jobs/adhoc/trigger", headers=OPERATOR)
            h.engine.error = None
            assert response.status == UNAVAILABLE
            assert body_of(response) == {"error": "scheduler failed; see the scheduler log"}

        h.run(body)
