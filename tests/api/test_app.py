"""Test the run endpoints: listing, detail, logs and the run actions."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_helpers import (
    AFTER_CHAIN,
    AT_1830,
    BAD_REQUEST,
    CONFLICT,
    EXTRACT_RUN,
    LOAD_RUN,
    NOT_FOUND,
    OK,
    OPERATOR,
    VIEWER,
    ApiHarness,
    MakeApi,
    body_of,
)
from pynet.messaging import JsonValue

from pyscheduler.engine.model import RunState

LOG_LINES = 300
DEFAULT_TAIL = 200
SHORT_TAIL = 3
ONE_MINUTE_SECONDS = 60.0


def _log_path(h: ApiHarness, run_id: str, attempt: int, ext: str = "out") -> Path:
    """Return the default log path of an attempt of a run on 2026-10-19."""
    job = run_id.split(".", 1)[0]
    return h.tmp_path / "logs" / job / "2026-10-19" / f"{run_id}.{attempt}.{ext}"


class TestListRuns:
    """List published and stored runs with filters."""

    def test_list_runs_filters(self, api: MakeApi) -> None:
        """Filter by run date, job and state; newest first; limit."""
        h = api()

        async def body() -> None:
            """Let the chain run, trigger ``adhoc``, then ask with filters."""
            await h.advance_to(AFTER_CHAIN)
            await h.client.post("/api/jobs/adhoc/trigger", headers=OPERATOR)
            await h.engine.settle()
            everything = body_of(await h.client.get("/api/runs", headers=VIEWER))["runs"]
            ids = [run["run_id"] for run in everything]
            assert set(ids) == {EXTRACT_RUN, LOAD_RUN, "adhoc.2026-10-19.1"}
            starts = [run["scheduled_at"] for run in everything]
            assert starts == sorted(starts, reverse=True)
            by_job = await h.client.get("/api/runs", query={"job": "load"}, headers=VIEWER)
            assert [run["run_id"] for run in body_of(by_job)["runs"]] == [LOAD_RUN]
            query = {"state": "succeeded", "run_date": "2026-10-19"}
            done = body_of(await h.client.get("/api/runs", query=query, headers=VIEWER))
            assert {run["run_id"] for run in done["runs"]} == {EXTRACT_RUN, LOAD_RUN}
            other_day = {"run_date": "2026-10-18"}
            empty = await h.client.get("/api/runs", query=other_day, headers=VIEWER)
            assert body_of(empty)["runs"] == []
            one = await h.client.get("/api/runs", query={"limit": "1"}, headers=VIEWER)
            assert len(body_of(one)["runs"]) == 1

        h.run(body)

    @pytest.mark.parametrize(
        "query",
        [
            {"run_date": "yesterday"},
            {"state": "sleepy"},
            {"limit": "0"},
            {"limit": "1001"},
            {"limit": "ten"},
            {"job": "bad name!"},
        ],
    )
    def test_bad_params_are_400(self, api: MakeApi, query: dict[str, str]) -> None:
        """Refuse a malformed filter with 400."""
        h = api()

        async def body() -> None:
            """Ask with the bad filter."""
            response = await h.client.get("/api/runs", query=query, headers=VIEWER)
            assert response.status == BAD_REQUEST
            assert "error" in body_of(response)

        h.run(body)

    def test_old_dates_come_from_the_store(self, api: MakeApi) -> None:
        """List a stored run whose date is older than the in-memory window."""
        h = api()

        async def body() -> None:
            """Backfill an old date, forget it from memory, and list it."""
            span: JsonValue = {"from": "2026-09-01", "to": "2026-09-01"}
            made = await h.client.post("/api/jobs/extract/backfill", json=span, headers=OPERATOR)
            assert made.status == OK
            await h.advance_to(h.clock.now().replace(hour=0, minute=10))
            old_id = "extract.2026-09-01.1"
            assert h.engine.book.published(old_id) is not None
            h.engine.book.drop(old_id)
            query = {"run_date": "2026-09-01", "job": "extract"}
            listed = await h.client.get("/api/runs", query=query, headers=VIEWER)
            assert [run["run_id"] for run in body_of(listed)["runs"]] == [old_id]
            both = await h.client.get("/api/runs", query={"run_date": "2026-09-01"}, headers=VIEWER)
            assert len(body_of(both)["runs"]) == len({old_id, "load.2026-09-01.1"})
            detail = await h.client.get(f"/api/runs/{old_id}", headers=VIEWER)
            assert body_of(detail)["run"]["state"] == "succeeded"

        h.run(body)


class TestRunDetail:
    """Show one run with its attempts and events."""

    def test_run_detail_has_attempts_and_events(self, api: MakeApi) -> None:
        """Return the run, its attempts and its events; unknown is 404."""
        h = api()

        async def body() -> None:
            """Let the chain run, then ask for ``extract``'s run."""
            await h.advance_to(AFTER_CHAIN)
            response = await h.client.get(f"/api/runs/{EXTRACT_RUN}", headers=VIEWER)
            assert response.status == OK
            detail = body_of(response)
            run = detail["run"]
            assert run["state"] == "succeeded"
            assert run["run_date"] == "2026-10-19"
            assert run["scheduled_at"] == "2026-10-19T18:30:00Z"
            assert run["trigger"] == "schedule"
            assert run["held_from"] is None
            assert run["duration_seconds"] == ONE_MINUTE_SECONDS
            [attempt] = detail["attempts"]
            assert attempt["outcome"] == "succeeded"
            assert attempt["exit_code"] == 0
            assert attempt["started_at"] == "2026-10-19T18:30:00Z"
            assert attempt["duration_seconds"] == ONE_MINUTE_SECONDS
            assert detail["events"]
            assert all(event["run_id"] == EXTRACT_RUN for event in detail["events"])
            assert all(isinstance(event["detail"], dict) for event in detail["events"])
            missing = await h.client.get("/api/runs/nope.2026-10-19.1", headers=VIEWER)
            assert missing.status == NOT_FOUND
            assert "error" in body_of(missing)

        h.run(body)


class TestActions:
    """Map the run actions onto the engine and its errors onto statuses."""

    def test_actions_map_errors(self, api: MakeApi) -> None:
        """Rerun of a running run is 409, of an unknown run 404, bad bodies 400."""
        h = api()

        async def body() -> None:
            """Start ``extract`` and try actions on it."""
            await h.advance_to(AT_1830 + timedelta(seconds=1))
            assert h.engine.book.get(EXTRACT_RUN) is not None
            url = f"/api/runs/{EXTRACT_RUN}"
            busy = await h.client.post(f"{url}/rerun", headers=OPERATOR)
            assert busy.status == CONFLICT
            assert "not final" in body_of(busy)["error"]
            unknown = await h.client.post("/api/runs/nope.2026-10-19.1/rerun", headers=OPERATOR)
            assert unknown.status == NOT_FOUND
            bogus = await h.client.post(f"{url}/explode", headers=OPERATOR)
            assert bogus.status == NOT_FOUND
            broken = await h.client.post(f"{url}/kill", body=b"{nope", headers=OPERATOR)
            assert broken.status == BAD_REQUEST
            wrong = await h.client.post(
                f"{url}/rerun", json={"downstream": "yes"}, headers=OPERATOR
            )
            assert wrong.status == BAD_REQUEST
            extra = await h.client.post(f"{url}/hold", json={"why": 1}, headers=OPERATOR)
            assert extra.status == BAD_REQUEST
            listed = await h.client.post(f"{url}/hold", json=[1], headers=OPERATOR)
            assert listed.status == BAD_REQUEST

        h.run(body)

    def test_each_action(self, api: MakeApi) -> None:
        """Kill and rerun ``extract``; walk a waiting ``load`` through the other actions."""
        h = api()

        async def act(run_id: str, action: str, **json: JsonValue) -> dict[str, Any]:
            """Post one action and return its JSON body (it must succeed)."""
            url = f"/api/runs/{run_id}/{action}"
            response = await h.client.post(url, json=json or None, headers=OPERATOR)
            assert response.status == OK, response.text
            await h.engine.settle()
            return body_of(response)

        async def body() -> None:
            """Act on ``extract`` and ``load``."""
            await h.client.post("/api/jobs/extract/trigger", headers=OPERATOR)
            await h.engine.settle()
            assert (await act(EXTRACT_RUN, "kill"))["run"]["run_id"] == EXTRACT_RUN
            await h.advance_to(h.clock.now().replace(minute=5))
            assert _state(h, EXTRACT_RUN) is RunState.FAILED
            reran = await act(EXTRACT_RUN, "rerun", downstream=True)
            assert reran["runs"][0]["run_id"] == EXTRACT_RUN
            await h.advance_to(h.clock.now().replace(minute=10))
            await act(EXTRACT_RUN, "mark-failed")
            made = body_of(await h.client.post("/api/jobs/load/trigger", headers=OPERATOR))
            load = made["run"]["run_id"]
            await h.engine.settle()
            assert _state(h, load) is not RunState.RUNNING
            assert (await act(load, "hold"))["run"]["state"] == "held"
            assert (await act(load, "release"))["run"]["state"] != "held"
            await act(load, "hold")
            assert (await act(load, "force-start"))["run"]["state"] == "queued"
            await h.advance_to(h.clock.now().replace(minute=20))
            assert (await act(load, "mark-failed"))["run"]["state"] == "failed"
            assert (await act(load, "mark-success"))["run"]["state"] == "succeeded"
            await act(load, "rerun")
            assert (await act(load, "skip"))["run"]["state"] == "skipped"

        h.run(body)


def _state(h: ApiHarness, run_id: str) -> RunState:
    """Return the in-memory state of ``run_id`` (it must exist)."""
    run = h.engine.book.get(run_id)
    assert run is not None
    return run.state


class TestLog:
    """Return the tail of an attempt's log file."""

    def test_log_tail(self, api: MakeApi) -> None:
        """Default tail of 200 lines of the latest attempt; tail, stream and errors."""
        h = api()

        async def body() -> None:
            """Run ``extract``, write its log, and read the tail."""
            await h.advance_to(AFTER_CHAIN)
            url = f"/api/runs/{EXTRACT_RUN}/log"
            missing = await h.client.get(url, headers=VIEWER)
            assert missing.status == NOT_FOUND
            assert body_of(missing)["error"] == "no log yet"
            path = _log_path(h, EXTRACT_RUN, 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"line {n}" for n in range(LOG_LINES)]
            path.write_bytes(("\n".join(lines) + "\n").encode() + b"bad \xff byte\n")
            tail = body_of(await h.client.get(url, headers=VIEWER))
            assert len(tail["lines"]) == DEFAULT_TAIL
            assert tail["lines"][-1] == "bad � byte"
            assert tail["lines"][0] == f"line {LOG_LINES + 1 - DEFAULT_TAIL}"
            assert tail["truncated"] is True
            assert tail["path"] == str(path)
            query = {"tail": str(SHORT_TAIL), "attempt": "1", "stream": "stdout"}
            short = body_of(await h.client.get(url, query=query, headers=VIEWER))
            assert short["lines"] == ["line 298", "line 299", "bad � byte"]
            everything = await h.client.get(url, query={"tail": "5000"}, headers=VIEWER)
            assert body_of(everything)["truncated"] is False
            assert len(body_of(everything)["lines"]) == LOG_LINES + 1
            err = await h.client.get(url, query={"stream": "stderr"}, headers=VIEWER)
            assert err.status == NOT_FOUND
            for bad in ({"tail": "5001"}, {"tail": "0"}, {"stream": "both"}, {"attempt": "x"}):
                response = await h.client.get(url, query=bad, headers=VIEWER)
                assert response.status == BAD_REQUEST, bad
            gone = await h.client.get(url, query={"attempt": "2"}, headers=VIEWER)
            assert gone.status == NOT_FOUND
            unknown = await h.client.get("/api/runs/nope.2026-10-19.1/log", headers=VIEWER)
            assert unknown.status == NOT_FOUND

        h.run(body)

    def test_merged_stderr_hint(self, api: MakeApi) -> None:
        """Answer 404 with a hint when stderr goes to the stdout file."""
        h = api(
            """
            jobs:
              m:
                command: run-m
                schedule: manual
                stderr: stdout
            """
        )

        async def body() -> None:
            """Trigger ``m`` and ask for its stderr."""
            made = body_of(await h.client.post("/api/jobs/m/trigger", headers=OPERATOR))
            run_id = made["run"]["run_id"]
            await h.engine.settle()
            url = f"/api/runs/{run_id}/log"
            response = await h.client.get(url, query={"stream": "stderr"}, headers=VIEWER)
            assert response.status == NOT_FOUND
            assert "stdout" in body_of(response)["error"]

        h.run(body)

    def test_no_attempt_yet(self, api: MakeApi) -> None:
        """Answer 404 ``no log yet`` for a run that has not started."""
        h = api()

        async def body() -> None:
            """Trigger ``load`` (it waits for ``extract``) and ask for its log."""
            made = body_of(await h.client.post("/api/jobs/load/trigger", headers=OPERATOR))
            run_id = made["run"]["run_id"]
            response = await h.client.get(f"/api/runs/{run_id}/log", headers=VIEWER)
            assert response.status == NOT_FOUND
            assert body_of(response)["error"] == "no log yet"

        h.run(body)
