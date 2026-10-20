"""Edge cases of the agent: forged and unsafe orders, repeats, early kills and failures."""

import logging
import signal
from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from agent_helpers import RUN_ID, World, lines, order, run_world, shell, until
from pynet.messaging import JsonValue

from pyscheduler.agents import worker_reports
from pyscheduler.agents.log_messages import LogRequest, log_request_payload
from pyscheduler.agents.messages import (
    ACK_TYPE,
    FINISHED_TYPE,
    HEARTBEAT_EVERY,
    HEARTBEAT_TYPE,
    HELLO_TYPE,
    KILL_TYPE,
    LOG_REQUEST_TYPE,
    STARTED_TYPE,
    WORK_TYPE,
    RunKey,
    heartbeat_payload,
    key_payload,
    kill_payload,
    parse_finished,
    work_payload,
)
from pyscheduler.agents.signing import seal
from pyscheduler.agents.worker import ADOPT_POLL
from pyscheduler.engine.launcher import LaunchSpec, MinderHandle, MinderLauncher

SECOND = 2
GATE = "go"
LOGGER = "pyscheduler.agents"


def _gated(tmp_path: Path, then: str = "echo done") -> str:
    """Return a script that waits for the ``go`` file, then runs ``then``."""
    return f"while [ ! -f {tmp_path / GATE} ]; do sleep 0.05; done; {then}"


async def _finished_signal(world: World) -> int | None:
    """Wait for the first ``finished`` report and return its signal."""
    _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
    return result.signal


class GatedLauncher(MinderLauncher):
    """A minder launcher that holds each launch until its gate opens."""

    def __init__(self) -> None:
        """Start with the gate shut."""
        super().__init__()
        self.gate = pynet.loop.Event()

    async def launch(self, spec: LaunchSpec) -> MinderHandle:
        """Wait for the gate, then launch as usual."""
        await self.gate.wait()
        return await super().launch(spec)


class TestRefused:
    """Unsigned, replayed and path-escaping orders are refused and never acted on."""

    def test_unsigned_and_replayed(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Refuse an order with no ``sig`` and a second copy of a valid one."""
        counter = tmp_path / "counter"
        caplog.set_level(logging.WARNING, logger=LOGGER)

        async def body(world: World) -> None:
            """Send an unsigned order, then one valid order twice."""
            payload = work_payload(shell(f"echo ran >> {counter}"))
            unsigned, _ = seal(WORK_TYPE, "build-01", payload, "s" * 40, now=world.clock.now())
            await world.scheduler.put_raw(unsigned, {})
            await until(lambda: "missing sig" in caplog.text)
            sealed, headers = seal(WORK_TYPE, "build-01", payload, "s" * 40, now=world.clock.now())
            await world.scheduler.put_raw(sealed, headers)
            await world.scheduler.wait_for(FINISHED_TYPE)
            await world.scheduler.put_raw(sealed, headers)
            await until(lambda: "replayed nonce" in caplog.text)
            assert len(world.scheduler.of(STARTED_TYPE)) == 1

        run_world(tmp_path, body)
        assert lines(counter) == ["ran"]

    @pytest.mark.parametrize("where", ["absolute", "relative"])
    def test_run_id_cannot_leave_folders(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, where: str
    ) -> None:
        """Refuse a run id holding a path, and write nothing outside the agent's folders."""
        outside = tmp_path / "outside"
        run_id = f"{outside}/x.2026-10-20.1" if where == "absolute" else "a/b.2026-10-20.1"
        caplog.set_level(logging.WARNING, logger=LOGGER)

        async def body(world: World) -> None:
            """Send the order with the unsafe run id."""
            payload: dict[str, JsonValue] = work_payload(order(["true"])) | {"run_id": run_id}
            await world.scheduler.send(WORK_TYPE, payload)
            await until(lambda: "malformed payload" in caplog.text)
            assert world.scheduler.of(STARTED_TYPE) == []

        run_world(tmp_path, body)
        assert not outside.exists()
        assert not (tmp_path / "logs" / "a").exists()
        assert {p.name for p in tmp_path.iterdir()} <= {"logs", "state"}


class TestRepeats:
    """A repeat order for a known attempt only repeats the news."""

    def test_repeat_while_running(self, tmp_path: Path) -> None:
        """Re-send only ``started`` for a repeat of an attempt still running."""
        counter = tmp_path / "counter"

        async def body(world: World) -> None:
            """Send the order twice while the job waits, then let it end."""
            work = shell(_gated(tmp_path, f"echo ran >> {counter}"))
            await world.scheduler.work(work)
            await world.scheduler.wait_for(STARTED_TYPE)
            await world.scheduler.work(work, fresh_id=True)
            await world.scheduler.wait_for(STARTED_TYPE, SECOND)
            assert world.scheduler.of(FINISHED_TYPE) == []
            (tmp_path / GATE).touch()
            await world.scheduler.wait_for(FINISHED_TYPE)
            assert len(world.scheduler.of(STARTED_TYPE)) == SECOND

        run_world(tmp_path, body)
        assert lines(counter) == ["ran"]

    def test_repeat_after_agent_restart(self, tmp_path: Path) -> None:
        """Remember an attempt across an agent restart and never run it again."""
        counter = tmp_path / "counter"

        async def body(world: World) -> None:
            """Finish and ack a job, restart the agent, then send the order again."""
            work = shell(f"echo ran >> {counter}")
            await world.scheduler.work(work)
            await world.scheduler.wait_for(FINISHED_TYPE)
            await world.scheduler.send(ACK_TYPE, key_payload(RUN_ID, 1))
            await until(lambda: not world.agent.pending)
            await world.restart_agent()
            await world.scheduler.wait_for(HELLO_TYPE, SECOND)
            await world.scheduler.work(work, fresh_id=True)
            await world.scheduler.wait_for(STARTED_TYPE, SECOND)
            await world.scheduler.wait_for(FINISHED_TYPE, SECOND)

        run_world(tmp_path, body)
        assert lines(counter) == ["ran"]


class TestKills:
    """Kills reach a minder that is still starting, and an adopted one."""

    def test_kill_while_launching(self, tmp_path: Path) -> None:
        """Apply a kill that arrives before the minder has started."""

        async def body(world: World) -> None:
            """Hold the launch, send the kill, then let the launch go."""
            launcher = GatedLauncher()
            world.agent._launcher = launcher
            key: RunKey = (RUN_ID, 1)
            await world.scheduler.work(order(["sleep", "30"]))
            await until(lambda: key in world.agent._launching)
            await world.scheduler.send(KILL_TYPE, kill_payload(RUN_ID, 1, "TERM"))
            await until(lambda: key in world.agent._kills)
            launcher.gate.set()
            assert await _finished_signal(world) == signal.SIGTERM

        run_world(tmp_path, body)

    def test_kill_adopted(self, tmp_path: Path) -> None:
        """Signal an adopted minder's group on ``kill TERM``."""
        status = tmp_path / "state" / "status" / f"{RUN_ID}.1.json"

        async def body(world: World) -> None:
            """Restart the agent mid-job, kill the job, and poll for its note."""
            await world.scheduler.work(shell(_gated(tmp_path)))
            await world.scheduler.wait_for(STARTED_TYPE)
            await world.restart_agent()
            await world.scheduler.wait_for(HELLO_TYPE, SECOND)
            await world.scheduler.send(KILL_TYPE, kill_payload(RUN_ID, 1, "TERM"))
            await until(status.exists)
            await world.clock.advance(timedelta(seconds=ADOPT_POLL))
            assert await _finished_signal(world) == signal.SIGTERM

        run_world(tmp_path, body)


class TestFailures:
    """Unexpected failures are logged, settled and survived."""

    def test_handler_failure_settles(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ack an order whose handling raised, logging only the error's type."""
        caplog.set_level(logging.WARNING, logger=LOGGER)

        async def boom(request: LogRequest) -> None:
            """Fail like a bug would."""
            raise RuntimeError(request.request_id)

        async def body(world: World) -> None:
            """Send a log request that blows up."""
            monkeypatch.setattr(world.agent, "_log", boom)
            request = LogRequest("cd" * 8, RUN_ID, 1, "stdout", "tail", count=1)
            await world.scheduler.send(LOG_REQUEST_TYPE, log_request_payload(request))
            await until(lambda: "cannot handle a message" in caplog.text)
            assert await world.scheduler.settled()

        run_world(tmp_path, body)
        assert "RuntimeError" in caplog.text
        assert "cd" * 8 not in caplog.text

    def test_launch_failure_is_a_start_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report any launch failure as ``finished`` with a start error."""

        async def broken(spec: LaunchSpec) -> MinderHandle:
            """Fail to launch with an unexpected error."""
            raise RuntimeError(spec.run_id)

        async def body(world: World) -> None:
            """Send work through a launcher that fails."""
            monkeypatch.setattr(world.agent._launcher, "launch", broken)
            await world.scheduler.work(order(["true"]))
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.start_error == RUN_ID
            assert world.agent.running == frozenset()

        run_world(tmp_path, body)

    def test_heartbeat_survives_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep beating after one beat fails."""
        calls: list[int] = []

        def flaky(running: frozenset[RunKey]) -> dict[str, JsonValue]:
            """Fail the first time only."""
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError
            return heartbeat_payload(running)

        monkeypatch.setattr(worker_reports, "heartbeat_payload", flaky)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        async def body(world: World) -> None:
            """Beat twice (once the heartbeat task has set its first alarm)."""
            await until(lambda: world.clock.next_due() is not None)
            await world.clock.advance(HEARTBEAT_EVERY)
            await until(lambda: "heartbeat failed" in caplog.text)
            await world.clock.advance(HEARTBEAT_EVERY)
            await world.scheduler.wait_for(HEARTBEAT_TYPE)

        run_world(tmp_path, body)
