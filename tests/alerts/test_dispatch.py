"""Tests for the dispatcher: pymq queues per notifier, retries, dead letters, resume, replay."""

from dataclasses import replace
from pathlib import Path

import pynet.loop
import pytest
from alert_helpers import (
    ALWAYS,
    SCALE,
    FlakyNotifier,
    RecordingNotifier,
    factory_for,
    load_text,
    make_alert,
    make_harness,
    pending,
    raise_alert,
    run_dispatcher,
)
from engine_helpers import START
from pymq.broker import TopicEvent

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import Criticality
from pyscheduler.notifiers import NotifyError
from pyscheduler.store.schema import Batch

CONFIG = """\
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

ONE_SEND = 1
TWO_SENDS = 2
THREE_SENDS = 3
SLOW_SECONDS = 2.0
QUICK_SECONDS = 0.5
POLL_SECONDS = 0.01
HOOK_URL = "https://hooks.example/abc"
HIDDEN_VALUE = "s3cr3t-token-value"

WITHOUT_DESK = """\
notifiers:
  pager:
    type: log
    path: logs/pager.log
    retries: {count: 3, delay: 1s}
routes:
  - {match: "alert.failed.#", notify: [pager]}
jobs:
  load: {command: "true", schedule: manual}
"""


def statuses(deliveries: list[DeliveryRecord]) -> dict[str, DeliveryStatus]:
    """Return each delivery's status by notifier."""
    return {d.notifier: d.status for d in deliveries}


async def stored(dispatcher: Dispatcher, alert_id: str) -> dict[str, DeliveryRecord]:
    """Settle the dispatcher, then return the alert's stored deliveries by notifier."""
    await dispatcher.settle()
    store = dispatcher.engine.store
    found = await pynet.loop.run_in_thread(store.deliveries_for, [alert_id])
    return {d.notifier: d for d in found.get(alert_id, [])}


class TestDelivery:
    """Each destination gets its copy, with retries and dead letters."""

    def test_delivers_to_each_destination(self, tmp_path: Path) -> None:
        """Send one alert to both routed notifiers; both rows end ``sent`` after one try."""
        harness = make_harness(tmp_path, CONFIG)
        pager, desk = RecordingNotifier(), RecordingNotifier()
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check both rows."""
            await raise_alert(harness, dispatcher, alert)
            rows = await stored(dispatcher, alert.alert_id)
            assert {n: (r.status, r.attempts) for n, r in rows.items()} == {
                "desk": (DeliveryStatus.SENT, ONE_SEND),
                "pager": (DeliveryStatus.SENT, ONE_SEND),
            }

        run_dispatcher(harness, {"pager": pager, "desk": desk}, body)
        assert [a["alert_id"] for a in pager.received] == [alert.alert_id]
        assert [a["alert_id"] for a in desk.received] == [alert.alert_id]
        assert pager.received[0]["scheduler"] == "sched-1"

    def test_retries_then_succeeds(self, tmp_path: Path) -> None:
        """Retry a notifier that fails twice; the third send succeeds and is recorded."""
        harness = make_harness(tmp_path, CONFIG)
        pager = FlakyNotifier(fails=2)
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check the pager's row."""
            await raise_alert(harness, dispatcher, alert)
            row = (await stored(dispatcher, alert.alert_id))["pager"]
            assert (row.status, row.attempts) == (DeliveryStatus.SENT, THREE_SENDS)

        run_dispatcher(harness, {"pager": pager, "desk": RecordingNotifier()}, body)
        assert pager.calls == THREE_SENDS

    def test_dead_after_retries(self, tmp_path: Path) -> None:
        """Give up after ``count + 1`` sends: the row is ``dead`` with the last error."""
        harness = make_harness(tmp_path, CONFIG)
        desk = FlakyNotifier(fails=ALWAYS, error="desk phone unplugged")
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check the desk's row, and the dead list."""
            await raise_alert(harness, dispatcher, alert)
            row = (await stored(dispatcher, alert.alert_id))["desk"]
            assert (row.status, row.attempts) == (DeliveryStatus.DEAD, TWO_SENDS)
            assert row.last_error == "desk phone unplugged"
            dead = await pynet.loop.run_in_thread(
                harness.store.deliveries_with, DeliveryStatus.DEAD
            )
            assert [d.delivery_id for d in dead] == [row.delivery_id]

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": desk}, body)
        assert desk.calls == TWO_SENDS

    def test_any_exception_is_a_failed_attempt(self, tmp_path: Path) -> None:
        """Count a non-``NotifyError`` from a notifier as a failed send, not a crash."""
        harness = make_harness(tmp_path, CONFIG)

        class Broken(RecordingNotifier):
            """A notifier that breaks with a plain ``RuntimeError``."""

            async def send(self, alert: object) -> None:
                """Raise ``RuntimeError``."""
                del alert
                msg = "no child watcher"
                raise RuntimeError(msg)

        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check the desk's row."""
            await raise_alert(harness, dispatcher, alert)
            row = (await stored(dispatcher, alert.alert_id))["desk"]
            assert (row.status, row.last_error) == (
                DeliveryStatus.DEAD,
                "RuntimeError: no child watcher",
            )

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": Broken()}, body)

    def test_send_is_bounded_by_the_notifier_timeout(self, tmp_path: Path) -> None:
        """Give up on a send that outlives the notifier's (real) timeout, as a failed try."""
        text = CONFIG.replace(
            "    retries: {count: 1, delay: 1s}", "    timeout: 1s\n    retries: {count: 0}"
        )
        harness = make_harness(tmp_path, text)
        stuck = RecordingNotifier(delay=SLOW_SECONDS * 5)
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check the desk's row."""
            await raise_alert(harness, dispatcher, alert)
            row = (await stored(dispatcher, alert.alert_id))["desk"]
            assert (row.status, row.last_error) == (
                DeliveryStatus.DEAD,
                "send timed out after 1s",
            )

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": stuck}, body)
        assert stuck.received == []

    def test_slow_notifier_does_not_block_others(self, tmp_path: Path) -> None:
        """Deliver to a quick notifier while another takes two seconds."""
        harness = make_harness(tmp_path, CONFIG)
        slow, quick = RecordingNotifier(delay=SLOW_SECONDS), RecordingNotifier()
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and time the quick notifier."""
            began = pynet.loop.get_running_loop().time()
            await raise_alert(harness, dispatcher, alert)
            for _ in range(int(QUICK_SECONDS / POLL_SECONDS)):
                if quick.times:
                    break
                await pynet.loop.sleep(POLL_SECONDS)
            assert quick.times
            assert quick.times[0] - began < QUICK_SECONDS
            assert slow.received == []
            rows = await stored(dispatcher, alert.alert_id)
            assert statuses(list(rows.values())) == {
                "desk": DeliveryStatus.SENT,
                "pager": DeliveryStatus.SENT,
            }

        run_dispatcher(harness, {"pager": slow, "desk": quick}, body)
        assert len(slow.received) == ONE_SEND  # the lease was kept alive: no second send

    def test_publishes_alert_topic(self, tmp_path: Path) -> None:
        """Publish each new alert on its topic for live subscribers."""
        harness = make_harness(tmp_path, CONFIG)
        seen: list[str] = []
        alert = make_alert(criticality=Criticality.NORMAL)

        def sink(sub_id: str, event: TopicEvent) -> None:
            """Remember the topic."""
            del sub_id
            seen.append(event.topic)

        async def body(dispatcher: Dispatcher) -> None:
            """Subscribe, then raise the alert."""
            assert dispatcher.broker is not None
            dispatcher.broker.subscribe("alert.#", sink)
            await raise_alert(harness, dispatcher, alert)
            await dispatcher.settle()

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": RecordingNotifier()}, body)
        assert seen == ["alert.failed.normal.load"]


class TestRedaction:
    """Stored errors never show an expanded ``${NAME}`` value."""

    def test_failed_delivery_error_is_redacted(self, tmp_path: Path) -> None:
        """Hide a webhook URL from ``${SLACK_URL}`` and a token a command echoes."""
        text = """\
            notifiers:
              ops-slack:
                type: slack
                webhook_url: "${SLACK_URL}"
                retries: {count: 0}
              script:
                type: command
                command: 'echo "token ${TOKEN}" >&2; exit 1'
                retries: {count: 0}
            routes:
              - {match: "alert.#", notify: [ops-slack, script]}
            jobs:
              load: {command: "true", schedule: manual}
            """
        env = {"SLACK_URL": HOOK_URL, "TOKEN": HIDDEN_VALUE}
        harness = make_harness(tmp_path, text, env)
        slack = FlakyNotifier(fails=ALWAYS, error=f"cannot reach {HOOK_URL}")
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check both stored errors."""
            await raise_alert(harness, dispatcher, alert)
            rows = await stored(dispatcher, alert.alert_id)
            assert rows["ops-slack"].last_error == "cannot reach ${SLACK_URL}"
            assert rows["script"].last_error == "command exited 1: token ${TOKEN}"

        run_dispatcher(harness, {"ops-slack": slack}, body)


class TestRestartAndReplay:
    """Pending deliveries resume after a restart; dead ones can be replayed."""

    def test_restart_resends_only_pending(self, tmp_path: Path) -> None:
        """Resend only the pending delivery, keeping its count of tries."""
        harness = make_harness(tmp_path, CONFIG)
        pager, desk = RecordingNotifier(), RecordingNotifier()
        alert = make_alert()
        sent, waiting = pending(alert, "desk", "pager")
        sent = replace(sent, status=DeliveryStatus.SENT, attempts=ONE_SEND)
        waiting = replace(waiting, attempts=ONE_SEND, last_error="busy")

        async def main() -> None:
            """Store the alert, then start a fresh dispatcher and let it work."""
            await harness.engine.writer.save(Batch(alerts=[alert], deliveries=[sent, waiting]))
            dispatcher = Dispatcher(
                harness.engine,
                factory=factory_for({"pager": pager, "desk": desk}),
                time_scale=SCALE,
            )
            await dispatcher.start()
            try:
                rows = await stored(dispatcher, alert.alert_id)
            finally:
                await dispatcher.stop()
            assert (rows["pager"].status, rows["pager"].attempts) == (
                DeliveryStatus.SENT,
                TWO_SENDS,
            )

        harness.run_engine(main)
        assert desk.received == []
        assert [a["alert_id"] for a in pager.received] == [alert.alert_id]

    def test_replay_dead(self, tmp_path: Path) -> None:
        """Replay a dead delivery to a healthy notifier; a sent one cannot be replayed."""
        harness = make_harness(tmp_path, CONFIG)
        desk = FlakyNotifier(fails=2)
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Kill the desk delivery, replay it, then try replaying a sent one."""
            await raise_alert(harness, dispatcher, alert)
            assert (await stored(dispatcher, alert.alert_id))["desk"].status is DeliveryStatus.DEAD
            replayed = await dispatcher.replay(alert.alert_id, "desk")
            assert [(d.notifier, d.status, d.attempts) for d in replayed] == [
                ("desk", DeliveryStatus.PENDING, 0)
            ]
            row = (await stored(dispatcher, alert.alert_id))["desk"]
            assert (row.status, row.attempts, row.last_error) == (DeliveryStatus.SENT, 1, "")
            with pytest.raises(LookupError, match=f"no dead delivery for {alert.alert_id}"):
                await dispatcher.replay(alert.alert_id)

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": desk}, body)
        assert desk.calls == THREE_SENDS

    def test_removed_notifier_goes_dead(self, tmp_path: Path) -> None:
        """Bury a pending retry whose notifier a reload removed."""
        text = CONFIG.replace("retries: {count: 1, delay: 1s}", "retries: {count: 1, delay: 100s}")
        harness = make_harness(tmp_path, text)
        desk = FlakyNotifier(fails=1)
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Fail the first send, reload without the desk, and let the retry come."""
            await raise_alert(harness, dispatcher, alert)
            while desk.calls == 0:
                await pynet.loop.sleep(POLL_SECONDS)
            new_config = load_text(tmp_path, WITHOUT_DESK)
            assert "desk" not in new_config.notifiers
            await harness.engine.reload(new_config, actor="test")
            row = (await stored(dispatcher, alert.alert_id))["desk"]
            assert (row.status, row.last_error) == (DeliveryStatus.DEAD, "notifier removed")

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": desk}, body)
        assert desk.calls == ONE_SEND

    def test_resumed_delivery_of_removed_notifier_goes_dead(self, tmp_path: Path) -> None:
        """Bury, at start, a pending delivery whose notifier is no longer configured."""
        harness = make_harness(tmp_path, CONFIG)
        alert = make_alert()
        (orphan,) = pending(alert, "gone", at=START)

        async def main() -> None:
            """Store the orphan, start a dispatcher, and read the row back."""
            await harness.engine.writer.save(Batch(alerts=[alert], deliveries=[orphan]))
            dispatcher = Dispatcher(harness.engine, factory=factory_for({}), time_scale=SCALE)
            await dispatcher.start()
            try:
                row = (await stored(dispatcher, alert.alert_id))["gone"]
            finally:
                await dispatcher.stop()
            assert (row.status, row.last_error) == (DeliveryStatus.DEAD, "notifier removed")

        harness.run_engine(main)


class TestStoreFailure:
    """A failed store write stops the dispatcher, quietly."""

    def test_store_failure_stops_delivering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop consuming after a write fails; later alerts are not sent."""
        harness = make_harness(tmp_path, CONFIG)
        pager, desk = RecordingNotifier(), RecordingNotifier()
        first, second = make_alert(), make_alert(attempt=2)

        async def body(dispatcher: Dispatcher) -> None:
            """Break the writer after storing the alerts, then enqueue them."""
            writer = harness.engine.writer
            for alert in (first, second):
                await writer.save(Batch(alerts=[alert], deliveries=pending(alert, "pager")))

            async def broken(batch: Batch) -> None:
                """Fail every save."""
                del batch
                msg = "disk on fire"
                raise NotifyError(msg)

            monkeypatch.setattr(writer, "save", broken)
            dispatcher.enqueue(first, pending(first, "pager"))
            await dispatcher.settle()
            assert isinstance(dispatcher.error, NotifyError)
            dispatcher.enqueue(second, pending(second, "pager"))
            await dispatcher.settle()

        run_dispatcher(harness, {"pager": pager, "desk": desk}, body)
        assert [a["alert_id"] for a in pager.received] == [first.alert_id]
