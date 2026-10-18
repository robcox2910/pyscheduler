"""Test the job endpoints, trigger and backfill, and the forecast."""

import pytest
from api_helpers import (
    AFTER_CHAIN,
    BAD_REQUEST,
    CONFLICT,
    EXTRACT_RUN,
    NOT_FOUND,
    OK,
    OPERATOR,
    VIEWER,
    MakeApi,
    body_of,
)
from pynet.messaging import JsonValue

FIVE = 5
THREE_DAYS = 3


class TestJobs:
    """List jobs and show one job in detail."""

    def test_list_jobs(self, api: MakeApi) -> None:
        """Summarise every job with its schedules and next firing."""
        h = api()

        async def body() -> None:
            """Ask for the job list."""
            response = await h.client.get("/api/jobs", headers=VIEWER)
            assert response.status == OK
            jobs = {job["name"]: job for job in body_of(response)["jobs"]}
            assert set(jobs) == {"extract", "load", "adhoc"}
            extract = jobs["extract"]
            assert extract["description"] == "Pull the trades"
            assert extract["owner"] == "ops"
            assert extract["tags"] == ["finance"]
            assert extract["criticality"] == "high"
            assert extract["enabled"] is True
            assert extract["calendar"] is None
            assert extract["schedules"] == ["cron 30 18 * * *"]
            assert extract["next_firing"] == "2026-10-19T18:30:00Z"
            assert jobs["adhoc"]["schedules"] == ["manual"]
            assert jobs["adhoc"]["next_firing"] is None
            assert jobs["load"]["depends_on"] == ["extract"]

        h.run(body)

    def test_job_detail(self, api: MakeApi) -> None:
        """Show the definition, the next five firings and the recent runs."""
        h = api()

        async def body() -> None:
            """Run the chain, then ask for ``extract``."""
            await h.advance_to(AFTER_CHAIN)
            response = await h.client.get("/api/jobs/extract", headers=VIEWER)
            assert response.status == OK
            job = body_of(response)
            assert job["name"] == "extract"
            definition = job["definition"]
            assert definition["command"] == "run-extract"
            assert definition["timezone"] == "UTC"
            assert definition["retries"]["count"] == 0
            assert definition["retries"]["delay_seconds"] == float(60)
            assert definition["overlap"] == "queue"
            assert definition["schedules"] == ["cron 30 18 * * *"]
            assert len(job["next_firings"]) == FIVE
            assert job["next_firings"][0] == "2026-10-20T18:30:00Z"
            assert [run["run_id"] for run in job["recent_runs"]] == [EXTRACT_RUN]
            missing = await h.client.get("/api/jobs/nope", headers=VIEWER)
            assert missing.status == NOT_FOUND

        h.run(body)

    def test_env_values_are_hidden(self, api: MakeApi) -> None:
        """Show the names of a job's environment variables but never their values."""
        h = api(
            """
            jobs:
              s:
                command: run-s
                schedule: manual
                env: {PASSWORD: hunter2}
            """
        )

        async def body() -> None:
            """Ask for the job."""
            response = await h.client.get("/api/jobs/s", headers=VIEWER)
            assert "hunter2" not in response.text
            assert body_of(response)["definition"]["env"] == {"PASSWORD": "***"}

        h.run(body)


class TestTriggerAndBackfill:
    """Create manual and backfill runs through the API."""

    def test_trigger_and_backfill(self, api: MakeApi) -> None:
        """Trigger with and without a run date; backfill a range; map the errors."""
        h = api()

        async def body() -> None:
            """Trigger ``adhoc`` twice and backfill ``extract``."""
            post = h.client.post
            first = body_of(await post("/api/jobs/adhoc/trigger", headers=OPERATOR))
            assert first["run"]["run_id"] == "adhoc.2026-10-19.1"
            assert first["run"]["trigger"] == "manual"
            dated = await post(
                "/api/jobs/adhoc/trigger", json={"run_date": "2026-10-16"}, headers=OPERATOR
            )
            assert body_of(dated)["run"]["run_date"] == "2026-10-16"
            span: JsonValue = {"from": "2026-10-14", "to": "2026-10-16"}
            filled = await post("/api/jobs/extract/backfill", json=span, headers=OPERATOR)
            assert filled.status == OK
            runs = body_of(filled)["runs"]
            assert len(runs) == THREE_DAYS
            assert {run["trigger"] for run in runs} == {"backfill"}
            unknown = await post("/api/jobs/nope/trigger", headers=OPERATOR)
            assert unknown.status == NOT_FOUND
            reversed_span: JsonValue = {"from": "2026-10-16", "to": "2026-10-14"}
            backwards = await post(
                "/api/jobs/extract/backfill", json=reversed_span, headers=OPERATOR
            )
            assert backwards.status == BAD_REQUEST
            bodies: list[JsonValue] = [
                {},
                {"from": "2026-10-14"},
                {"from": "x", "to": "y"},
                {"from": 1, "to": 2},
            ]
            for bad in bodies:
                response = await post("/api/jobs/extract/backfill", json=bad, headers=OPERATOR)
                assert response.status == BAD_REQUEST, bad
            bad_date = await post(
                "/api/jobs/adhoc/trigger", json={"run_date": "soon"}, headers=OPERATOR
            )
            assert bad_date.status == BAD_REQUEST

        h.run(body)

    def test_trigger_disabled_is_409(self, api: MakeApi) -> None:
        """Refuse to trigger a disabled job with 409."""
        h = api(
            """
            jobs:
              off:
                command: run-off
                schedule: manual
                enabled: false
            """
        )

        async def body() -> None:
            """Trigger the disabled job."""
            response = await h.client.post("/api/jobs/off/trigger", headers=OPERATOR)
            assert response.status == CONFLICT

        h.run(body)


class TestForecast:
    """Predict the runs of a window."""

    def test_forecast_endpoint(self, api: MakeApi) -> None:
        """Default to the next 24 hours; accept a window; refuse bad ones."""
        h = api()

        async def body() -> None:
            """Ask for forecasts."""
            response = await h.client.get("/api/forecast", headers=VIEWER)
            assert response.status == OK
            entries = body_of(response)["entries"]
            assert [entry["job"] for entry in entries] == ["extract", "load"]
            extract, load = entries
            assert extract["expected_start"] == "2026-10-19T18:30:00Z"
            assert extract["run_date"] == "2026-10-19"
            assert extract["via"] is None
            assert load["via"] == "extract"
            window = {"from": "2026-10-20T00:00:00Z", "to": "2026-10-21T00:00:00+00:00"}
            later = body_of(await h.client.get("/api/forecast", query=window, headers=VIEWER))
            assert later["entries"][0]["run_date"] == "2026-10-20"

        h.run(body)

    @pytest.mark.parametrize(
        "query",
        [
            {"from": "2026-10-20T00:00:00"},
            {"from": "tomorrow"},
            {"from": "2026-10-20T00:00:00Z", "to": "2026-10-19T00:00:00Z"},
            {"from": "2026-10-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
        ],
    )
    def test_bad_windows_are_400(self, api: MakeApi, query: dict[str, str]) -> None:
        """Refuse naive, malformed, reversed and over-long windows."""
        h = api()

        async def body() -> None:
            """Ask for the bad window."""
            response = await h.client.get("/api/forecast", query=query, headers=VIEWER)
            assert response.status == BAD_REQUEST

        h.run(body)
