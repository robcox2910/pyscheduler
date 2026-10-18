"""Test the alert endpoints: list, filter, redact, dead deliveries and replay."""

from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import timedelta
from typing import Any

from alert_helpers import RAISED, make_alert
from api_helpers import (
    BAD_REQUEST,
    CONFLICT,
    EXTRACT_RUN,
    FORBIDDEN,
    NOT_FOUND,
    OK,
    OPERATOR,
    START,
    VIEWER,
    ApiHarness,
    MakeApi,
    body_of,
    tokens_yaml,
)
from pyweb.testing import TestClient

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.api import TokenHolder, TokenTable, build_app, config_actions
from pyscheduler.config.model import AlertKind
from pyscheduler.store.schema import Batch

UNAVAILABLE = 503
TWO = 2
SECRET = "hunter2-hunter2"  # noqa: S105 -- a test value

CONFIG = """
notifiers:
  rec: {type: log, path: logs/rec.log}
  hook: {type: webhook, url: "https://example.org/${HOOK_KEY}"}
jobs:
  extract:
    command: run-extract
    schedule: "30 18 * * *"
  load:
    command: run-load
    depends_on: [extract]
  adhoc:
    command: run-adhoc
    schedule: manual
"""

type Body = Callable[[TestClient, Dispatcher], Coroutine[Any, Any, None]]


def delivery(
    alert: AlertRecord, status: DeliveryStatus, error: str = "", notifier: str = "rec"
) -> DeliveryRecord:
    """Return a delivery row of ``alert`` in ``status``."""
    return DeliveryRecord(alert.alert_id, notifier, status, 1, error, START)


def seed(h: ApiHarness, *alerts: tuple[AlertRecord, list[DeliveryRecord]]) -> None:
    """Write alerts and their deliveries straight to the store."""
    for alert, rows in alerts:
        h.store.apply(Batch(alerts=[alert], deliveries=rows))


def serve_alerts(h: ApiHarness, body: Body) -> None:
    """Run ``body`` with a client whose app has a started dispatcher."""
    dispatcher = Dispatcher(h.engine)
    path = h.tmp_path / "tokens.yaml"
    path.write_text(tokens_yaml(), encoding="utf-8")
    tokens = TokenHolder(TokenTable.load(path))
    reload, validate = config_actions(h.engine, h.config_path, env={"HOOK_KEY": SECRET})
    app = build_app(
        h.engine, tokens=tokens, reload=reload, validate=validate, dispatcher=dispatcher
    )
    client = TestClient(app)

    async def main() -> None:
        """Start the dispatcher, run the body, stop the dispatcher."""
        await dispatcher.start()
        try:
            await body(client, dispatcher)
        finally:
            await dispatcher.stop()

    h.run(main)


class TestListing:
    """``GET /api/alerts`` lists alerts with their deliveries."""

    def test_list_and_filters(self, api: MakeApi) -> None:
        """Filter by kind, job and since; newest first; the limit is checked."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})
        old = make_alert(AlertKind.FAILED, run_id="extract.2026-10-17.1", job="extract")
        mid = make_alert(
            AlertKind.TOO_LONG, run_id="load.2026-10-18.1", job="load",
            raised_at=RAISED + timedelta(days=1),
        )  # fmt: skip
        new = make_alert(
            AlertKind.FAILED, run_id="adhoc.2026-10-19.1", job="adhoc",
            raised_at=RAISED + timedelta(days=2),
        )  # fmt: skip
        seed(
            h,
            (old, [delivery(old, DeliveryStatus.SENT)]),
            (mid, [delivery(mid, DeliveryStatus.PENDING)]),
            (new, []),
        )

        async def body(client: TestClient, dispatcher: Dispatcher) -> None:
            """Ask with each filter."""
            del dispatcher
            everything = body_of(await client.get("/api/alerts", headers=VIEWER))["alerts"]
            assert [a["job"] for a in everything] == ["adhoc", "load", "extract"]
            first = everything[2]
            assert first["kind"] == "failed"
            assert first["scheduler"] == "pysched"
            assert [d["status"] for d in first["deliveries"]] == ["sent"]
            assert set(first["deliveries"][0]) == {
                "notifier", "status", "attempts", "last_error", "updated_at",
            }  # fmt: skip
            assert everything[0]["deliveries"] == []
            failed = await client.get("/api/alerts?kind=failed", headers=VIEWER)
            assert [a["job"] for a in body_of(failed)["alerts"]] == ["adhoc", "extract"]
            load = await client.get("/api/alerts?job=load", headers=VIEWER)
            assert [a["kind"] for a in body_of(load)["alerts"]] == ["too_long"]
            since = await client.get("/api/alerts?since=2026-10-18", headers=VIEWER)
            assert [a["job"] for a in body_of(since)["alerts"]] == ["adhoc", "load"]
            limited = await client.get("/api/alerts?limit=1", headers=VIEWER)
            assert len(body_of(limited)["alerts"]) == 1

        serve_alerts(h, body)

    def test_bad_parameters(self, api: MakeApi) -> None:
        """A bad kind, limit or date is a 400 naming the problem."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})

        async def body(client: TestClient, dispatcher: Dispatcher) -> None:
            """Ask with bad parameters."""
            del dispatcher
            kind = await client.get("/api/alerts?kind=nope", headers=VIEWER)
            assert kind.status == BAD_REQUEST
            assert body_of(kind)["error"].startswith("kind must be one of failed, retrying")
            for query in ("limit=0", "limit=1001", "since=yesterday"):
                response = await client.get(f"/api/alerts?{query}", headers=VIEWER)
                assert response.status == BAD_REQUEST

        serve_alerts(h, body)

    def test_redacts_expanded_values(self, api: MakeApi) -> None:
        """A ``${ENV}`` value in a message or an error is shown as ``${NAME}``."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})
        alert = make_alert()
        alert = replace(alert, message=f"cannot reach {SECRET}")
        seed(h, (alert, [delivery(alert, DeliveryStatus.DEAD, f"401 for {SECRET}", "hook")]))

        async def body(client: TestClient, dispatcher: Dispatcher) -> None:
            """Neither endpoint shows the secret."""
            del dispatcher
            listed = await client.get("/api/alerts", headers=VIEWER)
            dead = await client.get("/api/alerts/dead", headers=VIEWER)
            assert SECRET not in listed.text
            assert SECRET not in dead.text
            row = body_of(listed)["alerts"][0]
            assert row["message"] == "cannot reach ${HOOK_KEY}"
            assert row["deliveries"][0]["last_error"] == "401 for ${HOOK_KEY}"

        serve_alerts(h, body)

    def test_no_dispatcher_is_503(self, api: MakeApi) -> None:
        """Without a dispatcher every alert route says alerts are not running."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})

        async def body() -> None:
            """Ask for each route."""
            for path in ("/api/alerts", "/api/alerts/dead"):
                response = await h.client.get(path, headers=VIEWER)
                assert response.status == UNAVAILABLE
                assert body_of(response) == {"error": "alerts are not running"}
            posted = await h.client.post("/api/alerts/x/replay", headers=OPERATOR, json={})
            assert posted.status == UNAVAILABLE

        h.run(body)


class TestDeadAndReplay:
    """``GET /api/alerts/dead`` and ``POST /api/alerts/{id}/replay``."""

    def test_dead_lists_dead_deliveries_with_alerts(self, api: MakeApi) -> None:
        """Only dead deliveries are listed, each with its alert."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})
        dead = make_alert()
        fine = make_alert(attempt=TWO)
        seed(
            h,
            (dead, [delivery(dead, DeliveryStatus.DEAD, "boom")]),
            (fine, [delivery(fine, DeliveryStatus.SENT)]),
        )

        async def body(client: TestClient, dispatcher: Dispatcher) -> None:
            """List the dead ones."""
            del dispatcher
            answer = body_of(await client.get("/api/alerts/dead", headers=VIEWER))
            [row] = answer["deliveries"]
            assert row["status"] == "dead"
            assert row["last_error"] == "boom"
            assert row["alert"]["alert_id"] == dead.alert_id

        serve_alerts(h, body)

    def test_replay_dead_delivery(self, api: MakeApi) -> None:
        """A replay answers pending, then the delivery is sent."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})
        alert = make_alert()
        seed(h, (alert, [delivery(alert, DeliveryStatus.DEAD, "boom")]))

        async def body(client: TestClient, dispatcher: Dispatcher) -> None:
            """Replay, then wait for the send."""
            path = f"/api/alerts/{alert.alert_id}/replay"
            response = await client.post(path, headers=OPERATOR, json={"notifier": "rec"})
            assert response.status == OK, response.text
            [row] = body_of(response)["deliveries"]
            assert (row["notifier"], row["status"], row["attempts"]) == ("rec", "pending", 0)
            await dispatcher.settle()
            listed = body_of(await client.get("/api/alerts", headers=VIEWER))["alerts"]
            assert listed[0]["deliveries"][0]["status"] == "sent"
            again = await client.post(path, headers=OPERATOR, json={})
            assert again.status == CONFLICT
            assert body_of(again) == {"error": f"no dead delivery for {alert.alert_id}"}

        serve_alerts(h, body)

    def test_replay_errors(self, api: MakeApi) -> None:
        """Unknown alert is 404; a viewer is 403; a non-string notifier is 400."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})
        alert = make_alert()
        seed(h, (alert, [delivery(alert, DeliveryStatus.DEAD, "boom")]))

        async def body(client: TestClient, dispatcher: Dispatcher) -> None:
            """Try each bad replay."""
            del dispatcher
            path = f"/api/alerts/{alert.alert_id}/replay"
            assert (
                await client.post("/api/alerts/nope/replay", headers=OPERATOR, json={})
            ).status == NOT_FOUND
            viewer = {**VIEWER, "Content-Type": "application/json"}
            assert (await client.post(path, headers=viewer, json={})).status == FORBIDDEN
            bad = await client.post(path, headers=OPERATOR, json={"notifier": 3})
            assert bad.status == BAD_REQUEST
            other = await client.post(path, headers=OPERATOR, json={"notifier": "hook"})
            assert other.status == CONFLICT

        serve_alerts(h, body)


class TestRunDetail:
    """``GET /api/runs/{id}`` includes the run's alerts."""

    def test_run_detail_has_alerts(self, api: MakeApi) -> None:
        """The run's alerts come with their deliveries."""
        h = api(CONFIG, env={"HOOK_KEY": SECRET})

        async def body() -> None:
            """Seed an alert for the extract run, then read the run."""
            await h.advance_to(START.replace(hour=18, minute=31))
            alert = make_alert(run_id=EXTRACT_RUN, job="extract")
            await h.engine.writer.save(
                Batch(alerts=[alert], deliveries=[delivery(alert, DeliveryStatus.SENT)])
            )
            answer = body_of(await h.client.get(f"/api/runs/{EXTRACT_RUN}", headers=VIEWER))
            [row] = answer["alerts"]
            assert row["alert_id"] == alert.alert_id
            assert row["deliveries"][0]["notifier"] == "rec"
            other = body_of(await h.client.get("/api/runs/load.2026-10-19.1", headers=VIEWER))
            assert other["alerts"] == []

        h.run(body)
