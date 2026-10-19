"""Test the job page and the run page: definition, history, attempts, events, alerts, log, source."""

import dataclasses
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alert_helpers import make_alert
from api_helpers import TODAY, ApiHarness, MakeApi, users_yaml
from ui_helpers import HOSTILE, find, parse, seed, sign_in, stored_run

from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import AlertKind
from pyscheduler.engine.model import Event, Outcome, RunState, make_run_id
from pyscheduler.store import Batch
from pyscheduler.store.schema import ConfigRecord
from pyscheduler.ui.sparkline import sparkline

OK = 200
NOT_FOUND = 404
MINUTE = timedelta(minutes=1)
SECRET = "s3cr3t-value"  # noqa: S105
HISTORY_RUNS = 4
FIRINGS = 10
BOX_WIDTH = 120
BOX_HEIGHT = 24
SMALL_WIDTH = 50

JOBS = """
jobs:
  etl:
    command: run-etl --password ${SECRET}
    description: Load the ledger
    schedule: "30 18 * * *"
    env: {API_KEY: "${SECRET}"}
    alerts: {on: [failed], notify: []}
  adhoc:
    command: run-adhoc
    schedule: manual
"""

RUN_ID = make_run_id("etl", TODAY, 1)
OLD_RUN_ID = make_run_id("etl", TODAY - timedelta(days=1), 1)


def _at(days_ago: int = 0, hour: int = 8) -> datetime:
    """Return ``hour`` o'clock UTC, ``days_ago`` days before today."""
    day = TODAY - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _site(api: MakeApi) -> ApiHarness:
    """Return a harness (secrets in the env, three kinds of user) with etl history seeded."""
    h_ = api(
        JOBS,
        env={"SECRET": SECRET},
        tokens=False,
        users=users_yaml(ada="admin", ola="operator", vic="viewer"),
    )
    history = [
        stored_run(
            "etl",
            RunState.SUCCEEDED,
            TODAY - timedelta(days=n),
            _at(n),
            took=n * MINUTE,
            outcome=Outcome.SUCCEEDED,
        )
        for n in range(1, HISTORY_RUNS + 1)
    ]
    run, attempts = stored_run(
        "etl",
        RunState.FAILED,
        TODAY,
        _at(),
        took=MINUTE,
        outcome=Outcome.FAILED,
        reason=f"boom {SECRET}",
    )
    second = dataclasses.replace(
        attempts[0], attempt=2, stdout_path="/srv/logs/out.log", stderr_path="/srv/logs/err.log"
    )
    first = dataclasses.replace(
        attempts[0],
        stdout_path="/srv/logs/out.log",
        stderr_path="/srv/logs/out.log",
        reason=f"exit {SECRET}",
    )
    run = dataclasses.replace(run, attempt=2)
    seed(h_, *history, (run, [first, second]))
    alert = make_alert(AlertKind.FAILED, run_id=RUN_ID, job="etl")
    h_.store.apply(
        Batch(
            events=[Event(_at(), "ola", RUN_ID, "hold", json.dumps({"why": SECRET}))],
            alerts=[alert],
            deliveries=[
                DeliveryRecord(
                    alert.alert_id, "rec", DeliveryStatus.DEAD, 3, f"bad {SECRET}", _at()
                )
            ],
        )
    )
    return h_


async def _get(h_: ApiHarness, path: str, who: str | None = "ola") -> Any:
    """GET ``path`` signed in as ``who`` (a fresh session each time)."""
    h_.client.cookies.clear()
    if who is not None:
        await sign_in(h_, who)
    return await h_.client.get(path)


class TestSparkline:
    """The sparkline is a tiny SVG with at most one path."""

    def test_sparkline(self) -> None:
        """No values: empty SVG; one: a flat line; many: scaled into the box."""
        assert "<path" not in sparkline([])
        assert f'viewBox="0 0 {BOX_WIDTH} {BOX_HEIGHT}"' in sparkline([])
        (flat,) = find(sparkline([5]), "path")
        ys = {part.split()[1] for part in flat[1]["d"].replace("M", "").split("L")}  # type: ignore[union-attr]
        assert len(ys) == 1
        many = find(sparkline([1, 3, 2], width=SMALL_WIDTH, height=BOX_HEIGHT), "path")
        points = [
            tuple(float(n) for n in part.split())
            for part in many[0][1]["d"].replace("M", "").split("L")  # type: ignore[union-attr]
        ]
        assert [x for x, _ in points] == [1.0, 25.0, 49.0]
        low, high, mid = points[0][1], points[1][1], points[2][1]
        assert high < mid < low  # bigger values are higher up (smaller y)
        assert low == BOX_HEIGHT - 1
        assert high == 1.0
        same = find(sparkline([2, 2, 2]), "path")
        assert len({p.split()[1] for p in same[0][1]["d"].replace("M", "").split("L")}) == 1  # type: ignore[union-attr]


class TestJobPage:
    """``/jobs/<name>``: definition, next firings, history, sparkline, buttons."""

    def test_job_page(self, api: MakeApi) -> None:
        """Show the definition, ten firings, the history and one sparkline path; 404 if unknown."""
        h_ = _site(api)

        async def body() -> None:
            """Open the page and an unknown job."""
            page = await _get(h_, "/jobs/etl")
            assert page.status == OK
            text = page.text
            assert "Load the ledger" in text
            (definition,) = find(text, "table", **{"class": "definition"})
            assert "run-etl --password ${SECRET}" in definition[2]
            (firings,) = find(text, "ol", id="firings")
            assert len(re.findall(r"\d{4}-\d{2}-\d{2} ", firings[2])) == FIRINGS
            history = [t for t in parse(text) if t[0] == "tr" and "data-run-id" in t[1]]
            assert history[0][1]["data-run-id"] == RUN_ID
            assert len(history) == HISTORY_RUNS + 1
            assert len(find(text, "path")) == 1
            assert (await _get(h_, "/jobs/nope")).status == NOT_FOUND
            assert (await _get(h_, "/jobs/etl", who=None)).status != OK

        h_.run(body)

    def test_operator_buttons(self, api: MakeApi) -> None:
        """Operators get trigger and backfill controls and run action buttons; viewers none."""
        h_ = _site(api)

        async def body() -> None:
            """Compare an operator's page with a viewer's."""
            seen: dict[str, list[str]] = {}
            for who in ("ola", "vic"):
                text = (await _get(h_, "/jobs/etl", who)).text
                seen[who] = [
                    e[1]["data-job-action"] or ""
                    for e in find(text, "button")
                    if "data-job-action" in e[1]
                ]
                actions = [e for e in find(text, "button") if "data-action" in e[1]]
                assert bool(actions) == (who == "ola")
            assert seen == {"ola": ["trigger", "backfill"], "vic": []}

        h_.run(body)


class TestRunPage:
    """``/runs/<id>``: summary, attempts, events, alerts and the log viewer."""

    def test_run_page(self, api: MakeApi) -> None:
        """Show the tables and the log viewer's data; 404 for an unknown run."""
        h_ = _site(api)

        async def body() -> None:
            """Open the run page as an operator, then as a viewer."""
            page = await _get(h_, f"/runs/{RUN_ID}")
            assert page.status == OK
            text = page.text
            attempts = [e for e in find(text, "tr") if "data-attempt" in e[1]]
            assert [e[1]["data-attempt"] for e in attempts] == ["1", "2"]
            (events,) = find(text, "table", id="events")
            assert "hold" in events[2]
            assert "ola" in events[2]
            (alerts,) = find(text, "table", id="alerts")
            assert "failed" in alerts[2]
            assert "rec: dead (3 tries)" in alerts[2]
            (log,) = find(text, "section", id="log")
            assert log[1]["data-run"] == RUN_ID
            assert log[1]["data-latest"] == "2"
            options = find(text, "option")
            assert [(o[1]["value"], o[1]["data-merged"]) for o in options] == [
                ("1", "1"),
                ("2", "0"),
            ]
            assert {e[1]["data-stream"] for e in find(text, "button") if "data-stream" in e[1]} == {
                "stdout",
                "stderr",
            }
            assert "/srv/logs" not in text  # server paths never leave the server
            assert "Definition" in text
            assert "run-etl" in text
            viewer = (await _get(h_, f"/runs/{RUN_ID}", "vic")).text
            assert not find(viewer, "table", id="alerts")
            assert not [e for e in find(viewer, "button") if "data-action" in e[1]]
            assert (await _get(h_, "/runs/nope.2026-10-19.1")).status == NOT_FOUND

        h_.run(body)

    def test_run_without_attempts_has_no_log_viewer(self, api: MakeApi) -> None:
        """A run that never started shows a note instead of the viewer."""
        h_ = _site(api)
        seed(h_, stored_run("adhoc", RunState.HELD, TODAY, _at()))

        async def body() -> None:
            """Open the page."""
            text = (await _get(h_, f"/runs/{make_run_id('adhoc', TODAY, 1)}")).text
            assert not find(text, "section", id="log")
            assert "no log yet" in text

        h_.run(body)

    def test_old_config_version_shows_source(self, api: MakeApi) -> None:
        """A run of an older version shows that version's file text, escaped, and a link."""
        h_ = _site(api)
        h_.engine.config_version = 2
        job = h_.config.jobs["etl"]
        sources = {job.source.file: f"jobs:\n  etl: {HOSTILE} old ${{SECRET}} {SECRET}\n"}
        h_.store.apply(Batch(configs=[ConfigRecord(1, _at(), "abc", json.dumps(sources))]))
        run, _ = stored_run("etl", RunState.SUCCEEDED, TODAY, _at(0, 9), seq=7)

        async def body() -> None:
            """Open the old run as operator and viewer, and a current one."""
            seed(h_, (run, []))
            page = (await _get(h_, f"/runs/{run.run_id}")).text
            (source,) = find(page, "pre", id="definition-source")
            assert HOSTILE in source[2]
            assert "<script>" not in page
            assert SECRET not in page
            assert find(page, "a", href="/admin/configs/1")
            assert not find(page, "table", **{"class": "definition"})
            viewer = (await _get(h_, f"/runs/{run.run_id}", "vic")).text
            assert not find(viewer, "pre", id="definition-source")
            assert find(viewer, "a", href="/admin/configs/1")
            h_.engine.config_version = 1
            current = (await _get(h_, f"/runs/{run.run_id}")).text
            assert find(current, "table", **{"class": "definition"})
            assert not find(current, "pre", id="definition-source")

        h_.run(body)

    def test_missing_config_version(self, api: MakeApi) -> None:
        """An older version that is not stored gets a note, not a crash."""
        h_ = _site(api)
        h_.engine.config_version = 2

        async def body() -> None:
            """Open the run."""
            text = (await _get(h_, f"/runs/{RUN_ID}")).text
            assert "no longer stored" in text

        h_.run(body)


class TestSecrets:
    """Expanded ``${NAME}`` values never reach either page."""

    @pytest.mark.parametrize("who", ["ola", "vic", "ada"])
    def test_secrets_redacted(self, api: MakeApi, who: str) -> None:
        """The secret shows as ``${SECRET}`` in the definition, reason, events and alerts."""
        h_ = _site(api)

        async def body() -> None:
            """Open both pages."""
            for path in ("/jobs/etl", f"/runs/{RUN_ID}", f"/runs/{OLD_RUN_ID}"):
                text = (await _get(h_, path, who)).text
                assert SECRET not in text, path
            job = (await _get(h_, "/jobs/etl", who)).text
            assert "${SECRET}" in job
            assert "***" in job  # the env value
            run = (await _get(h_, f"/runs/{RUN_ID}", who)).text
            assert "boom ${SECRET}" in run

        h_.run(body)
