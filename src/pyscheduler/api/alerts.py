"""The alert endpoints: list alerts, list dead deliveries, replay one.

Alerts are read from the store on a helper thread, with the delivery of
each alert to each notifier next to it. Every string is shown with the
config's ``${NAME}`` values hidden. Replaying needs the dispatcher (it
posts the copies again), so with no dispatcher every alert route answers
503 ``alerts are not running``.
"""

from datetime import UTC, datetime, time
from typing import cast

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest

from pyscheduler.alerts.delivery import NOT_RUNNING, AlertsStoppedError
from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus
from pyscheduler.alerts.routing import alert_json
from pyscheduler.api.serialize import instant, redact
from pyscheduler.api.support import (
    BAD_REQUEST,
    UNAVAILABLE,
    ApiError,
    guarded,
    json_body,
    not_found,
    query_date,
    query_int,
    query_name,
)
from pyscheduler.config.model import AlertKind
from pyscheduler.engine.core import Engine

__all__ = ["add_alert_routes", "alert_rows", "dead_rows", "delivery_json"]

type JsonObject = dict[str, JsonValue]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
CONFLICT = 409


def delivery_json(delivery: DeliveryRecord) -> JsonObject:
    """Return one notifier's delivery as JSON (the error is as stored: redact it on the way out)."""
    return {
        "notifier": delivery.notifier,
        "status": delivery.status.value,
        "attempts": delivery.attempts,
        "last_error": delivery.last_error,
        "updated_at": instant(delivery.updated_at),
    }


def _alert_view(engine: Engine, alert: AlertRecord) -> JsonObject:
    """Return the alert as the JSON notifiers get (the ``scheduler`` name included)."""
    return alert_json(alert, scheduler=engine.config.settings.name)


def alert_rows(engine: Engine, alerts: list[AlertRecord]) -> list[JsonValue]:
    """Return each alert as JSON with its ``deliveries``, with expanded values hidden.

    Reads the store: call it on a helper thread.
    """
    found = engine.store.deliveries_for([a.alert_id for a in alerts])
    rows: list[JsonValue] = []
    for alert in alerts:
        row = _alert_view(engine, alert)
        row["deliveries"] = [delivery_json(d) for d in found.get(alert.alert_id, [])]
        rows.append(row)
    return cast("list[JsonValue]", redact(rows, engine.config.expansions))


def dead_rows(engine: Engine, limit: int) -> list[JsonValue]:
    """Return the ``dead`` deliveries, each with its alert (read the store: use a thread)."""
    store = engine.store
    rows: list[JsonValue] = []
    for delivery in store.deliveries_with(DeliveryStatus.DEAD, limit):
        alert = store.get_alert(delivery.alert_id)
        if alert is None:
            continue
        row = delivery_json(delivery)
        row["alert"] = _alert_view(engine, alert)
        rows.append(row)
    return cast("list[JsonValue]", redact(rows, engine.config.expansions))


def _kind(request: HttpRequest) -> AlertKind | None:
    """Return the ``kind`` query parameter.

    Raises:
        ApiError: 400 if it is not an alert kind.

    """
    text = request.query.get("kind")
    if text is None:
        return None
    try:
        return AlertKind(text)
    except ValueError:
        names = ", ".join(k.value for k in AlertKind)
        raise ApiError(BAD_REQUEST, f"kind must be one of {names}") from None


def add_alert_routes(app: App, engine: Engine, dispatcher: Dispatcher | None) -> None:
    """Register the ``/api/alerts`` endpoints on ``app``."""

    def running() -> Dispatcher:
        """Return the dispatcher, or raise 503 when alerts are not running."""
        if dispatcher is None:
            raise ApiError(UNAVAILABLE, NOT_RUNNING)
        return dispatcher

    @app.get("/api/alerts")
    @guarded
    async def list_alerts(request: HttpRequest) -> JsonObject:
        """List alerts (newest first) filtered by ``kind``, ``job`` and ``since``, with deliveries."""
        running()
        kind = _kind(request)
        job = query_name(request, "job")
        day = query_date(request, "since")
        since = None if day is None else datetime.combine(day, time(), tzinfo=UTC)
        limit = query_int(request, "limit", default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
        store = engine.store

        def read() -> list[JsonValue]:
            """Read the matching alerts and their deliveries."""
            found = store.query_alerts(kind=kind, job=job, since=since, limit=limit)
            return alert_rows(engine, found)

        return {"alerts": await run_in_thread(read)}

    @app.get("/api/alerts/dead")
    @guarded
    async def dead_alerts(request: HttpRequest) -> JsonObject:
        """List the dead deliveries (oldest first), each with its alert."""
        running()
        limit = query_int(request, "limit", default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
        return {"deliveries": await run_in_thread(dead_rows, engine, limit)}

    @app.post("/api/alerts/<alert_id>/replay")
    @guarded
    async def replay_alert(request: HttpRequest, alert_id: str) -> JsonObject:
        """Send an alert's dead deliveries again (all of them, or just ``notifier``'s)."""
        posting = running()
        body = json_body(request, ("notifier",))
        notifier = body.get("notifier")
        if notifier is not None and not isinstance(notifier, str):
            raise ApiError(BAD_REQUEST, "notifier must be a string")
        if await run_in_thread(engine.store.get_alert, alert_id) is None:
            msg = f"unknown alert {alert_id!r}"
            raise not_found(msg)
        try:
            fresh = await posting.replay(alert_id, notifier)
        except LookupError as exc:
            raise ApiError(CONFLICT, str(exc)) from None
        except AlertsStoppedError:
            raise ApiError(UNAVAILABLE, NOT_RUNNING) from None
        rows = cast("list[JsonValue]", [delivery_json(d) for d in fresh])
        return {"deliveries": redact(rows, engine.config.expansions)}
