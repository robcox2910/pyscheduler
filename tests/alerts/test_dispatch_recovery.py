"""Tests for the dispatcher across restarts, replays, reloads and store trouble."""

from dataclasses import replace
from pathlib import Path

import pynet.loop
import pytest
from alert_helpers import (
    DISPATCH_CONFIG,
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
    stored,
)
from engine_helpers import START

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.model import DeliveryStatus
from pyscheduler.store.schema import Batch

ONE_SEND = 1
TWO_SENDS = 2
THREE_SENDS = 3
POLL_SECONDS = 0.01
SLOW_WRITE_SECONDS = 0.5

SLOW_RETRY = DISPATCH_CONFIG.replace("{count: 1, delay: 1s}", "{count: 1, delay: 100s}")
"""The dispatch config with the desk waiting one (scaled) second before its retry."""

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


class TestRestartAndReplay:
    """Pending deliveries resume after a restart; dead ones can be replayed."""

    def test_restart_resends_only_pending(self, tmp_path: Path) -> None:
        """Resend only the pending delivery, keeping its count of tries."""
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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

    def test_concurrent_replays_send_once(self, tmp_path: Path) -> None:
        """Post a dead delivery once when two replays of it race."""
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
        desk = FlakyNotifier(fails=2)
        alert = make_alert()

        async def body(dispatcher: Dispatcher) -> None:
            """Kill the desk delivery, then replay it twice at once."""
            await raise_alert(harness, dispatcher, alert)
            assert (await stored(dispatcher, alert.alert_id))["desk"].status is DeliveryStatus.DEAD
            results = await pynet.loop.gather(
                dispatcher.replay(alert.alert_id, "desk"),
                dispatcher.replay(alert.alert_id, "desk"),
                return_exceptions=True,
            )
            assert sorted(type(r).__name__ for r in results) == ["LookupError", "list"]
            assert (await stored(dispatcher, alert.alert_id))["desk"].status is DeliveryStatus.SENT

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": desk}, body)
        assert desk.calls == THREE_SENDS

    def test_removed_notifier_goes_dead(self, tmp_path: Path) -> None:
        """Bury a pending retry whose notifier a reload removed."""
        harness = make_harness(tmp_path, SLOW_RETRY)
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
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
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

    def test_changed_retries_get_a_fresh_queue(self, tmp_path: Path) -> None:
        """Post to ``notify.desk.1`` after a reload changes its retries; the old queue drains."""
        harness = make_harness(tmp_path, SLOW_RETRY)
        desk = FlakyNotifier(fails=1)
        first, second = make_alert(), make_alert(attempt=2)

        async def body(dispatcher: Dispatcher) -> None:
            """Fail the first send, reload with new retries, raise a second alert."""
            await raise_alert(harness, dispatcher, first)
            while desk.calls == 0:
                await pynet.loop.sleep(POLL_SECONDS)
            changed = SLOW_RETRY.replace("{count: 1, delay: 100s}", "{count: 2, delay: 100s}")
            await harness.engine.reload(load_text(tmp_path, changed), actor="test")
            await raise_alert(harness, dispatcher, second)
            for alert in (first, second):
                row = (await stored(dispatcher, alert.alert_id))["desk"]
                assert row.status is DeliveryStatus.SENT
            broker = dispatcher.broker
            assert broker is not None
            assert (broker.stats("notify.desk").acked, broker.stats("notify.desk.1").acked) == (
                ONE_SEND,
                ONE_SEND,
            )

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": desk}, body)
        assert desk.calls == THREE_SENDS


class TestStoreFailure:
    """A failed store write fails the engine stop; a slow one loses no lease."""

    def test_store_failure_fails_the_engine_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail the engine stop and stop consuming after a write fails."""
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
        pager, desk = RecordingNotifier(), RecordingNotifier()
        first, second = make_alert(), make_alert(attempt=2)
        failure = OSError("disk on fire")

        async def body(dispatcher: Dispatcher) -> None:
            """Break the writer after storing the alerts, then enqueue them."""
            writer = harness.engine.writer
            for alert in (first, second):
                await writer.save(Batch(alerts=[alert], deliveries=pending(alert, "pager")))

            async def broken(batch: Batch) -> None:
                """Fail every save."""
                del batch
                raise failure

            monkeypatch.setattr(writer, "save", broken)
            dispatcher.enqueue(first, pending(first, "pager"))
            await dispatcher.settle()
            assert dispatcher.error is failure
            assert harness.engine.error is failure
            assert await harness.engine.wait_failed() is failure
            dispatcher.enqueue(second, pending(second, "pager"))
            await dispatcher.settle()

        run_dispatcher(harness, {"pager": pager, "desk": desk}, body)
        assert [a["alert_id"] for a in pager.received] == [first.alert_id]

    def test_replay_store_failure_fails_the_engine_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store failure while replaying stops the dispatcher and the engine, then re-raises."""
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
        desk = FlakyNotifier(fails=2)
        alert = make_alert()
        failure = OSError("disk on fire")

        async def body(dispatcher: Dispatcher) -> None:
            """Kill the desk delivery, break the writer, and replay."""
            await raise_alert(harness, dispatcher, alert)
            assert (await stored(dispatcher, alert.alert_id))["desk"].status is DeliveryStatus.DEAD

            async def broken(batch: Batch) -> None:
                """Fail every save."""
                del batch
                raise failure

            monkeypatch.setattr(harness.engine.writer, "save", broken)
            with pytest.raises(OSError, match="disk on fire"):
                await dispatcher.replay(alert.alert_id, "desk")
            assert dispatcher.error is failure
            assert harness.engine.error is failure

        run_dispatcher(harness, {"pager": RecordingNotifier(), "desk": desk}, body)

    def test_lease_is_kept_during_a_slow_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the lease while a slow store write runs, so nothing is sent twice."""
        harness = make_harness(tmp_path, DISPATCH_CONFIG)
        pager, desk = RecordingNotifier(), RecordingNotifier()
        alert = make_alert()
        writer = harness.engine.writer
        save = writer.save

        async def slow(batch: Batch) -> None:
            """Save, but only after much longer than a (scaled) lease."""
            await pynet.loop.sleep(SLOW_WRITE_SECONDS)
            await save(batch)

        monkeypatch.setattr(writer, "save", slow)

        async def body(dispatcher: Dispatcher) -> None:
            """Raise the alert and check both rows."""
            await raise_alert(harness, dispatcher, alert)
            rows = await stored(dispatcher, alert.alert_id)
            assert {n: (r.status, r.attempts) for n, r in rows.items()} == {
                "desk": (DeliveryStatus.SENT, ONE_SEND),
                "pager": (DeliveryStatus.SENT, ONE_SEND),
            }

        run_dispatcher(harness, {"pager": pager, "desk": desk}, body)
        assert (len(pager.received), len(desk.received)) == (ONE_SEND, ONE_SEND)
