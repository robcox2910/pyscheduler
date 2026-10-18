"""Test reload: swapping in a new config while runs are open."""

import json
import textwrap
from datetime import timedelta

import pytest
from engine_helpers import START, TODAY, Harness, MakeHarness

from pyscheduler.config import Config, load_config
from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import Event, RunState
from pyscheduler.store import Batch
from pyscheduler.store.schema import event_from_row
from pyscheduler.testing import FakeResult

ONE_MINUTE = timedelta(minutes=1)
TEN_MINUTES = timedelta(minutes=10)
FOUR_HOURS = timedelta(hours=4)
AT_1830 = START.replace(hour=18, minute=30)
AT_2000 = START.replace(hour=20)
ALICE = "alice"
SECOND_VERSION = 2
UNAVAILABLE = 503
DISK_FULL = "disk full"

CHAIN = """
jobs:
  a:
    command: run-a
    schedule: "30 18 * * *"
    retries: {count: 3, delay: 1m}
  b:
    command: run-b
    depends_on: [a]
"""


def new_config(h: Harness, text: str) -> Config:
    """Load ``text`` with the harness's ``scheduler:`` section, as a reload would."""
    lines = (h.tmp_path / "pysched.yaml").read_text(encoding="utf-8").splitlines()
    scheduler = [lines[0]]
    for line in lines[1:]:
        if not line.startswith("  "):
            break
        scheduler.append(line)
    path = h.tmp_path / "reloaded.yaml"
    path.write_text("\n".join(scheduler) + "\n" + text.lstrip("\n"), encoding="utf-8")
    return load_config(path, env={}, today=TODAY)


def all_events(h: Harness) -> list[Event]:
    """Return every event in the store, oldest first (scheduler-wide ones included)."""
    with h.reopened() as store:
        records = sorted(store._events.select(), key=lambda r: r.record_id)
        return [event_from_row(record.data) for record in records]


class TestReload:
    """Open runs adopt the new config; removed jobs' runs are cancelled."""

    def test_reload_adopts_new_definition(self, harness: MakeHarness) -> None:
        """Launch a waiting run with the reloaded command, and record version 2."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(duration=TEN_MINUTES))
        changed = new_config(h, CHAIN.replace("command: run-b", "command: [run-b2, --fast]"))

        async def body() -> None:
            """Start ``a``, reload while ``b`` waits, let both run."""
            await h.advance_to(AT_1830)
            await h.engine.reload(changed, actor=ALICE)
            assert h.engine.config is changed
            assert h.run("b").config_version == SECOND_VERSION
            await h.advance(TEN_MINUTES + ONE_MINUTE)

        h.run_engine(body)
        assert h.launcher.launched[-1].argv == ("run-b2", "--fast")
        assert h.run("b").state is RunState.SUCCEEDED
        with h.reopened() as store:
            record = store.latest_config()
        assert record is not None
        assert record.version == SECOND_VERSION
        assert record.digest == changed.digest

    def test_reload_removed_job_cancels_open_runs(self, harness: MakeHarness) -> None:
        """Cancel the open runs of a removed job and of a disabled one."""
        three = CHAIN + "  c:\n    command: run-c\n    depends_on: [a]\n"
        h = harness(three)
        h.launcher.plan("a", FakeResult(duration=TEN_MINUTES))
        text = CHAIN + "  c:\n    command: run-c\n    depends_on: [a]\n    enabled: false\n"
        changed = new_config(h, text.replace("  b:\n    command: run-b\n    depends_on: [a]\n", ""))

        async def body() -> None:
            """Start ``a`` while ``b`` and ``c`` wait, then reload without them."""
            await h.advance_to(AT_1830)
            await h.engine.reload(changed, actor=ALICE)
            await h.advance(TEN_MINUTES + ONE_MINUTE)

        h.run_engine(body)
        assert h.run("b").state is RunState.CANCELLED
        assert h.run("b").reason == "removed by reload"
        assert h.run("c").state is RunState.CANCELLED
        assert h.run("c").reason == "disabled"
        assert h.run("a").state is RunState.SUCCEEDED
        assert [spec.run_id.split(".")[0] for spec in h.launcher.launched] == ["a"]

    def test_reload_running_attempt_keeps_old(self, harness: MakeHarness) -> None:
        """Let a running attempt of a removed job finish, with no retry."""
        h = harness(CHAIN)
        h.launcher.plan("a", FakeResult(duration=TEN_MINUTES, exit_code=1))
        changed = new_config(h, "jobs:\n  z:\n    command: run-z\n    schedule: manual\n")

        async def body() -> None:
            """Start ``a``, reload without it, let the attempt fail."""
            await h.advance_to(AT_1830)
            await h.engine.reload(changed, actor=ALICE)
            assert h.run("a").state is RunState.RUNNING
            assert h.run("a").config_version == 1
            await h.advance(FOUR_HOURS)

        h.run_engine(body)
        assert h.run("a").state is RunState.FAILED
        with h.reopened() as store:
            assert len(store.attempts_for("a.2026-10-19.1")) == 1
        assert h.runs("a", TODAY + timedelta(days=1)) == []

    def test_reload_rebuilds_firings(self, harness: MakeHarness) -> None:
        """Fire a job added by a reload, at its new time, and not the removed one."""
        h = harness(CHAIN)
        changed = new_config(h, 'jobs:\n  z:\n    command: run-z\n    schedule: "0 19 * * *"\n')

        async def body() -> None:
            """Reload at noon, then move past both times."""
            await h.advance_to(START.replace(hour=12))
            await h.engine.reload(changed, actor=ALICE)
            await h.advance_to(AT_2000)

        h.run_engine(body)
        assert h.runs("a") == []
        assert h.run("z").scheduled_at == START.replace(hour=19)

    def test_reload_requeues_with_new_pools(self, harness: MakeHarness) -> None:
        """Re-queue a queued run with its new pool need, so it starts when that pool has room."""
        text = """
            pools: {db: 1, other: 1}
            jobs:
              hog:
                command: run-hog
                schedule: manual
                pools: {db: 1}
              q:
                command: run-q
                schedule: manual
                pools: {db: 1}
            """
        h = harness(text)
        h.launcher.plan("hog", FakeResult(duration=FOUR_HOURS))
        changed = new_config(
            h, textwrap.dedent(text).replace("pools: {db: 1}\n", "pools: {other: 1}\n")[1:]
        )

        async def body() -> None:
            """Fill ``db``, queue ``q``, move ``q`` to ``other`` by reload."""
            await h.engine.trigger("hog", actor=ALICE)
            queued = await h.engine.trigger("q", actor=ALICE)
            assert queued.state is RunState.QUEUED
            await h.engine.reload(changed, actor=ALICE)
            await h.engine.settle()
            assert h.run("q").state is RunState.RUNNING

        h.run_engine(body)

    def test_reload_reevaluates_queued_runs(self, harness: MakeHarness) -> None:
        """Send a queued run back to waiting when the reload gives it a dependency."""
        text = """
            pools: {db: 1}
            jobs:
              hog:
                command: run-hog
                schedule: manual
                pools: {db: 1}
              b:
                command: run-b
                schedule: manual
                pools: {db: 1}
              c:
                command: run-c
                schedule: manual
            """
        h = harness(text)
        h.launcher.plan("hog", FakeResult(duration=TEN_MINUTES))
        plain = textwrap.dedent(text)[1:]
        changed = new_config(h, plain.replace("run-b\n", "run-b\n    depends_on: [c]\n"))

        async def body() -> None:
            """Queue ``b`` behind ``hog``, then reload."""
            await h.engine.trigger("hog", actor=ALICE)
            queued = await h.engine.trigger("b", actor=ALICE)
            assert queued.state is RunState.QUEUED
            await h.engine.reload(changed, actor=ALICE)
            assert h.run("b").state is RunState.WAITING
            assert queued.run_id not in h.engine.ready
            await h.advance(FOUR_HOURS)

        h.run_engine(body)
        assert h.run("b").state is RunState.WAITING
        assert h.engine.admission.running == 0

    def test_reload_commit_fails_503(
        self, harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answer 503 when the reload's commit fails."""
        h = harness(CHAIN)
        changed = new_config(h, CHAIN.replace("run-b", "run-b4"))
        real = h.store.apply

        def apply(batch: Batch) -> None:
            """Fail the commit that carries a config record."""
            if batch.configs:
                raise OSError(DISK_FULL)
            real(batch)

        monkeypatch.setattr(h.store, "apply", apply)

        async def body() -> None:
            """Reload, and see the failure."""
            with pytest.raises(ActionError) as caught:
                await h.engine.reload(changed, actor=ALICE)
            assert caught.value.status == UNAVAILABLE

        h.run_engine(body)
        assert isinstance(h.engine.error, OSError)

    def test_reload_same_digest_is_noop(self, harness: MakeHarness) -> None:
        """Do nothing (no new version) when the config did not change."""
        h = harness(CHAIN)

        async def body() -> None:
            """Reload the same text."""
            await h.engine.reload(h.config, actor=ALICE)
            await h.engine.settle()

        h.run_engine(body)
        assert h.engine.config_version == 1
        with h.reopened() as store:
            assert store.latest_config() is None

    def test_reload_event_and_limits(self, harness: MakeHarness) -> None:
        """Record one ``reload`` event and swap in the new cap."""
        h = harness(CHAIN)
        changed = new_config(h, CHAIN.replace("run-b", "run-b3"))

        async def body() -> None:
            """Reload, then reload again after stopping."""
            await h.engine.reload(changed, actor=ALICE)
            await h.engine.settle()
            await h.engine.stop()
            with pytest.raises(ActionError) as caught:
                await h.engine.reload(h.config, actor=ALICE)
            assert caught.value.status == UNAVAILABLE

        h.run_engine(body)
        reloads = [event for event in all_events(h) if event.action == "reload"]
        assert len(reloads) == 1
        assert reloads[0].actor == ALICE
        assert reloads[0].run_id is None
        detail = json.loads(reloads[0].detail)
        assert (detail["before"], detail["after"]) == (1, SECOND_VERSION)
        assert h.engine.admission.max_running == changed.settings.max_running
