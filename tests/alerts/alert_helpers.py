"""Build alerts and a small config for the alert tests."""

import textwrap
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pynet.loop
from engine_helpers import START, TODAY, Harness
from pynet.messaging import JsonValue

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import AlertRecord, DeliveryRecord, DeliveryStatus, alert_id_for
from pyscheduler.alerts.routing import destinations
from pyscheduler.config import Config, load_config
from pyscheduler.config.model import AlertKind, Criticality, NotifierDef, Settings
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.notifiers import Notifier, NotifierFactory, NotifyError, build_notifier
from pyscheduler.store import Store
from pyscheduler.store.schema import Batch
from pyscheduler.testing import FakeLauncher

RAISED = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
RUN_DATE = date(2026, 10, 17)

CONFIG = """\
notifiers:
  team-log: {type: log, path: logs/alerts.log}
  ops-slack: {type: slack, webhook_url: "https://hooks.example/abc"}
routes:
  - {match: "alert.*.critical.#", notify: [ops-slack]}
  - {match: "alert.failed.#", notify: [team-log]}
jobs:
  load:
    command: "true"
    schedule: manual
    alerts: {notify: [team-log]}
"""


def make_config(tmp_path: Path) -> Config:
    """Load the sample config (one job ``load`` that notifies ``team-log``)."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    return load_config(path, env={})


def make_alert(
    kind: AlertKind = AlertKind.FAILED,
    *,
    run_id: str | None = "load.20261017.1",
    job: str | None = "load",
    criticality: Criticality = Criticality.CRITICAL,
    attempt: int = 1,
    raised_at: datetime = RAISED,
) -> AlertRecord:
    """Return an alert; with ``run_id=None`` it is a run-less one."""
    alert_id = (
        alert_id_for(run_id, kind, attempt)
        if run_id is not None
        else f"_scheduler:{kind}:{raised_at:%Y%m%dT%H%M%S%fZ}"
    )
    return AlertRecord(
        alert_id=alert_id,
        kind=kind,
        run_id=run_id,
        job=job,
        run_date=None if run_id is None else RUN_DATE,
        attempt=attempt,
        criticality=criticality,
        raised_at=raised_at,
        message="load failed",
        detail={"exit_code": 2, "files": ["a", "b"]},
    )


# -- the dispatcher's test notifiers and harness ---------------------------

ALWAYS = 1_000_000
"""A number of failures no test ever reaches: the notifier always fails."""


class RecordingNotifier:
    """Remember every alert sent, and when (loop time); optionally take a while."""

    def __init__(self, delay: float = 0.0) -> None:
        """Start with nothing received; each send first sleeps ``delay`` seconds."""
        self.delay = delay
        self.received: list[Mapping[str, JsonValue]] = []
        self.times: list[float] = []

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Sleep the delay, then record the alert."""
        if self.delay:
            await pynet.loop.sleep(self.delay)
        self.received.append(alert)
        self.times.append(pynet.loop.get_running_loop().time())


class FlakyNotifier(RecordingNotifier):
    """Fail the first ``fails`` sends with ``NotifyError(error)``, then record."""

    def __init__(self, fails: int, error: str = "cannot reach the phone") -> None:
        """Fail ``fails`` times (``ALWAYS``: every time) with ``error``."""
        super().__init__()
        self.fails = fails
        self.error = error
        self.calls = 0

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Count the call; fail while failures are left, else record."""
        self.calls += 1
        if self.calls <= self.fails:
            raise NotifyError(self.error)
        await super().send(alert)


def factory_for(notifiers: Mapping[str, Notifier]) -> NotifierFactory:
    """Return a factory giving each named notifier; other names are built for real."""

    def factory(definition: NotifierDef, settings: Settings) -> Notifier:
        """Return the test notifier named like ``definition``, else build it."""
        found = notifiers.get(definition.name)
        return found if found is not None else build_notifier(definition, settings)

    return factory


def write_config(tmp_path: Path, text: str) -> Path:
    """Write a config: a ``scheduler:`` section for ``tmp_path``, then ``text``."""
    header = (
        "scheduler:\n"
        "  name: sched-1\n"
        f"  state_dir: {tmp_path / 'state'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n"
        "  kill_grace: 10s\n"
    )
    path = tmp_path / "pysched.yaml"
    path.write_text(header + textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def load_text(tmp_path: Path, text: str, env: Mapping[str, str] | None = None) -> Config:
    """Write and load a config (see ``write_config``)."""
    return load_config(write_config(tmp_path, text), env=dict(env or {}), today=TODAY)


def make_harness(tmp_path: Path, text: str, env: Mapping[str, str] | None = None) -> Harness:
    """Build an engine harness (store, fake clock, fake launcher) for ``text``."""
    config = load_text(tmp_path, text, env)
    store = Store.open(tmp_path / "state", sync=False)
    clock = FakeClock(START)
    launcher = FakeLauncher(clock)
    engine = Engine(config, store, clock=clock, launcher=launcher)
    return Harness(tmp_path, config, store, clock, launcher, engine)


def pending(alert: AlertRecord, *notifiers: str, at: datetime = START) -> list[DeliveryRecord]:
    """Return a fresh ``pending`` delivery of ``alert`` for each notifier."""
    return [
        DeliveryRecord(alert.alert_id, name, DeliveryStatus.PENDING, 0, "", at)
        for name in notifiers
    ]


def run_dispatcher(
    harness: Harness,
    notifiers: Mapping[str, Notifier],
    body: Callable[[Dispatcher], Coroutine[Any, Any, None]],
) -> None:
    """Run ``body`` with a started dispatcher (scale ``SCALE``) inside a running engine."""

    async def main() -> None:
        """Start the dispatcher, run the body, stop the dispatcher."""
        dispatcher = Dispatcher(harness.engine, factory=factory_for(notifiers), time_scale=SCALE)
        await dispatcher.start()
        try:
            await body(dispatcher)
        finally:
            await dispatcher.stop()

    harness.run_engine(main)


SCALE = 0.01
"""How much faster than real time the dispatcher's tests wait."""


async def raise_alert(
    harness: Harness, dispatcher: Dispatcher, alert: AlertRecord
) -> list[DeliveryRecord]:
    """Store ``alert`` with a pending delivery per destination, then enqueue it."""
    deliveries = pending(alert, *destinations(harness.engine.config, alert))
    await harness.engine.writer.save(Batch(alerts=[alert], deliveries=deliveries))
    dispatcher.enqueue(alert, deliveries)
    return deliveries


DISPATCH_CONFIG = """\
notifiers:
  pager:
    type: log
    path: logs/pager.log
    retries: {count: 3, delay: 1s}
  desk:
    type: log
    path: logs/desk.log
    retries: {count: 1, delay: 1s}
routes:
  - {match: "alert.failed.#", notify: [desk, pager]}
jobs:
  load: {command: "true", schedule: manual}
"""
"""Two log notifiers: ``pager`` (3 retries) and ``desk`` (1 retry), both routed."""


async def stored(dispatcher: Dispatcher, alert_id: str) -> dict[str, DeliveryRecord]:
    """Settle the dispatcher, then return the alert's stored deliveries by notifier."""
    await dispatcher.settle()
    store = dispatcher.engine.store
    found = await pynet.loop.run_in_thread(store.deliveries_for, [alert_id])
    return {d.notifier: d for d in found.get(alert_id, [])}
