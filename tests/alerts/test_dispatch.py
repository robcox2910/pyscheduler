"""Tests for the dispatcher: pymq queues per notifier, retries, dead letters, resume, replay."""

from pathlib import Path

import pynet.loop
from alert_helpers import (
    ALWAYS,
    DISPATCH_CONFIG,
    FlakyNotifier,
    RecordingNotifier,
    make_alert,
    make_harness,
    raise_alert,
    run_dispatcher,
    stored,
)
from pymq.broker import TopicEvent

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.config.model import Criticality

ONE_SEND = 1
TWO_SENDS = 2
THREE_SENDS = 3
SLOW_SECONDS = 2.0
QUICK_SECONDS = 0.5
POLL_SECONDS = 0.01
HOOK_URL = "https://hooks.example/abc"
HIDDEN_VALUE = "s3cr3t-token-value"


def statuses(deliveries: list[DeliveryRecord]) -> dict[str, DeliveryStatus]:
    """Return each delivery's status by notifier."""
    return {d.notifier: d.status for d in deliveries}


class TestDelivery:
    """Each destination gets its copy, with retries and dead letters."""

    def test_delivers_to_each_destination(self, tmp_path: Path) -> None:
        """Send one alert to both routed notifiers; both rows end ``sent`` after one try."""
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)

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
        text = DISPATCH_CONFIG.replace(
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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
