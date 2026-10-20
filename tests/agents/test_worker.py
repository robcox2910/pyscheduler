"""Tests for the agent: real broker, a pretend scheduler, and real jobs under the minder."""

import logging
import signal
from datetime import timedelta
from pathlib import Path

import pytest
from agent_helpers import (
    NAME,
    RUN_DATE,
    RUN_ID,
    SECRET,
    TAGS,
    WRONG_SECRET,
    World,
    journal_of,
    lines,
    make_agent,
    now_clock,
    order,
    run_world,
    shell,
    until,
)

from pyscheduler.agents.journal import RUNNING, Entry
from pyscheduler.agents.log_messages import LogRequest, log_request_payload, parse_log_reply
from pyscheduler.agents.messages import (
    ACK_TYPE,
    FINISHED_TYPE,
    HEARTBEAT_EVERY,
    HEARTBEAT_TYPE,
    HELLO_TYPE,
    KILL_TYPE,
    LOG_REPLY_TYPE,
    LOG_REQUEST_TYPE,
    STARTED_TYPE,
    WORK_TYPE,
    key_payload,
    kill_payload,
    parse_finished,
    parse_heartbeat,
    parse_hello,
    parse_started,
    work_payload,
)
from pyscheduler.agents.worker import ADOPT_POLL, MINDER_GONE

STALE = timedelta(minutes=10)
TAIL_COUNT = 2
READ_FROM = 2
READ_LIMIT = 100
SECOND = 2
THIRD = 3
GATE = "go"


def _gated(tmp_path: Path) -> str:
    """Return a script that waits for the ``go`` file, then prints ``done``."""
    gate = tmp_path / GATE
    return f"while [ ! -f {gate} ]; do sleep 0.05; done; echo done"


class TestWork:
    """Signed work runs once under the minder and is reported."""

    def test_runs_work_and_reports(self, tmp_path: Path) -> None:
        """Report ``started`` then ``finished`` with exit 0; stdout holds ``hi``."""

        async def body(world: World) -> None:
            """Send ``echo hi`` and read the reports."""
            hello = await world.scheduler.wait_for(HELLO_TYPE)
            assert parse_hello(hello.payload) == (TAGS, 4, set())
            await world.scheduler.work(shell("echo hi"))
            started = await world.scheduler.wait_for(STARTED_TYPE)
            finished = await world.scheduler.wait_for(FINISHED_TYPE)
            key, out, err = parse_started(started.payload)
            assert key == (RUN_ID, 1)
            assert out == str(tmp_path / "logs" / "nightly" / RUN_DATE / f"{RUN_ID}.1.out")
            assert err.endswith(f"{RUN_ID}.1.err")
            done_key, result = parse_finished(finished.payload)
            assert done_key == (RUN_ID, 1)
            assert result.exit_code == 0
            assert result.succeeded
            assert lines(Path(out)) == ["hi"]
            assert started.agent == finished.agent == NAME

        run_world(tmp_path, body)

    def test_agent_rejects_forged_work(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Run nothing and send nothing for a wrong secret, a stale or a stranger's order."""
        marker = tmp_path / "ran"
        work = work_payload(shell(f"touch {marker}"))
        caplog.set_level(logging.WARNING, logger="pyscheduler.agents")

        async def body(world: World) -> None:
            """Send three forgeries, then a valid log request to know they were handled."""
            scheduler = world.scheduler
            await scheduler.wait_for(HELLO_TYPE)
            await scheduler.send(WORK_TYPE, work, secret=WRONG_SECRET)
            await scheduler.send(WORK_TYPE, work, at=world.clock.now() - STALE)
            await scheduler.send(WORK_TYPE, work, agent="build-02")
            await until(lambda: caplog.text.count("rejected") >= THIRD)
            request = LogRequest("ab" * 8, RUN_ID, 1, "stdout", "tail", count=1)
            await scheduler.send(LOG_REQUEST_TYPE, log_request_payload(request))
            reply = await scheduler.wait_for(LOG_REPLY_TYPE)
            assert parse_log_reply(reply.payload).missing
            assert {m.kind for m in scheduler.reports} == {HELLO_TYPE, LOG_REPLY_TYPE}

        run_world(tmp_path, body)
        assert not marker.exists()
        for reason in ("bad signature", "stale", "unknown agent"):
            assert reason in caplog.text
        assert SECRET not in caplog.text
        assert WRONG_SECRET not in caplog.text
        assert str(marker) not in caplog.text

    def test_malformed_work_is_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Drop a validly signed order whose payload is not a work order."""
        caplog.set_level(logging.WARNING, logger="pyscheduler.agents")

        async def body(world: World) -> None:
            """Send a work message with a path-like job name."""
            await world.scheduler.wait_for(HELLO_TYPE)
            bad = work_payload(order(["true"])) | {"job": "../etc"}
            await world.scheduler.send(WORK_TYPE, bad)
            await until(lambda: "malformed payload" in caplog.text)

        run_world(tmp_path, body)
        assert journal_of(tmp_path).running() == []

    def test_start_error_is_reported(self, tmp_path: Path) -> None:
        """Report a job that cannot start (missing cwd) as finished with a start error."""
        work = order(["true"])
        payload = work_payload(work) | {"cwd": str(tmp_path / "nowhere")}

        async def body(world: World) -> None:
            """Send the order and read the reports."""
            await world.scheduler.send(WORK_TYPE, payload)
            await world.scheduler.wait_for(STARTED_TYPE)
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.start_error is not None

        run_world(tmp_path, body)

    def test_duplicate_dispatch_runs_once(self, tmp_path: Path) -> None:
        """Run a re-sent order once; a repeat only re-sends ``started`` and ``finished``."""
        counter = tmp_path / "counter"
        work = shell(f"echo ran >> {counter}")

        async def body(world: World) -> None:
            """Dispatch, re-dispatch inside the dedupe window, then on a fresh broker."""
            scheduler = world.scheduler
            assert await scheduler.work(work)
            await scheduler.wait_for(FINISHED_TYPE)
            assert not await scheduler.work(work)  # the broker drops it as a duplicate
            await world.restart_broker()
            await until(lambda: len(scheduler.of(HELLO_TYPE)) >= SECOND)
            assert await scheduler.work(work)  # a fresh broker has forgotten the id
            await scheduler.wait_for(STARTED_TYPE, SECOND)
            again = await scheduler.wait_for(FINISHED_TYPE, SECOND)
            assert parse_finished(again.payload)[0] == (RUN_ID, 1)

        run_world(tmp_path, body)
        assert lines(counter) == ["ran"]


class TestReports:
    """Finished reports are re-sent until acked; heartbeats carry what runs."""

    def test_pending_until_acked(self, tmp_path: Path) -> None:
        """Re-send ``finished`` after a reconnect and on heartbeats until ``finished_ack``."""

        async def body(world: World) -> None:
            """Finish a job, reconnect, beat, ack, then beat and reconnect again."""
            scheduler, agent = world.scheduler, world.agent
            await scheduler.work(order(["true"]))
            await scheduler.wait_for(FINISHED_TYPE)
            await world.restart_broker()
            await scheduler.wait_for(FINISHED_TYPE, SECOND)
            await world.clock.advance(HEARTBEAT_EVERY)
            await scheduler.wait_for(FINISHED_TYPE, THIRD)
            beat = await scheduler.wait_for(HEARTBEAT_TYPE)
            assert parse_heartbeat(beat.payload) == set()
            await scheduler.send(ACK_TYPE, key_payload(RUN_ID, 1))
            await until(lambda: not agent.pending)
            await world.clock.advance(HEARTBEAT_EVERY)
            await world.clock.advance(HEARTBEAT_EVERY)
            await scheduler.wait_for(HEARTBEAT_TYPE, THIRD)
            hellos = len(scheduler.of(HELLO_TYPE))
            await world.restart_broker()
            await scheduler.wait_for(HELLO_TYPE, hellos + 1)
            await world.clock.advance(HEARTBEAT_EVERY)
            await scheduler.wait_for(HEARTBEAT_TYPE, THIRD + 1)
            assert len(scheduler.of(FINISHED_TYPE)) == THIRD

        run_world(tmp_path, body)

    def test_heartbeat_lists_running(self, tmp_path: Path) -> None:
        """List a running attempt in the heartbeat and in a new hello."""

        async def body(world: World) -> None:
            """Start a gated job and beat while it runs."""
            await world.scheduler.work(shell(_gated(tmp_path)))
            await world.scheduler.wait_for(STARTED_TYPE)
            await world.clock.advance(HEARTBEAT_EVERY)
            beat = await world.scheduler.wait_for(HEARTBEAT_TYPE)
            assert parse_heartbeat(beat.payload) == {(RUN_ID, 1)}
            (tmp_path / GATE).touch()
            await world.scheduler.wait_for(FINISHED_TYPE)

        run_world(tmp_path, body)


class TestKillAndLogs:
    """Kill orders signal the job; log requests read the agent's files."""

    def test_kill(self, tmp_path: Path) -> None:
        """End a ``sleep 30`` job with signal 15 on ``kill TERM``."""

        async def body(world: World) -> None:
            """Start the sleep, then send the kill."""
            await world.scheduler.work(order(["sleep", "30"]))
            await world.scheduler.wait_for(STARTED_TYPE)
            await world.scheduler.send(KILL_TYPE, kill_payload(RUN_ID, 1, "TERM"))
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.signal == signal.SIGTERM

        run_world(tmp_path, body)

    def test_kill_signal_and_unknown(self, tmp_path: Path) -> None:
        """Ignore a kill for an attempt not running here; ``kill KILL`` ends with signal 9."""

        async def body(world: World) -> None:
            """Start the sleep, kill a stranger, then kill it."""
            await world.scheduler.work(order(["sleep", "30"]))
            await world.scheduler.wait_for(STARTED_TYPE)
            await world.scheduler.send(KILL_TYPE, kill_payload(RUN_ID, SECOND, "KILL"))
            await world.scheduler.send(KILL_TYPE, kill_payload(RUN_ID, 1, "KILL"))
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.signal == signal.SIGKILL

        run_world(tmp_path, body)

    def test_log_request_tail_and_read(self, tmp_path: Path) -> None:
        """Answer ``tail`` with lines and ``read`` with base64 bytes; unknown logs are missing."""

        async def body(world: World) -> None:
            """Print three lines, then ask for them three ways."""
            scheduler = world.scheduler
            await scheduler.work(shell("printf 'a\\nb\\nc\\n'"))
            await scheduler.wait_for(FINISHED_TYPE)
            asks = [
                LogRequest("01" * 8, RUN_ID, 1, "stdout", "tail", count=TAIL_COUNT),
                LogRequest(
                    "02" * 8, RUN_ID, 1, "stdout", "read", offset=READ_FROM, limit=READ_LIMIT
                ),
                LogRequest("03" * 8, RUN_ID, SECOND, "stdout", "tail", count=1),
            ]
            for ask in asks:
                await scheduler.send(LOG_REQUEST_TYPE, log_request_payload(ask))
            await scheduler.wait_for(LOG_REPLY_TYPE, THIRD)
            found = [parse_log_reply(m.payload) for m in scheduler.of(LOG_REPLY_TYPE)]
            replies = {r.request_id: r for r in found}
            tail, read, unknown = (replies[a.request_id] for a in asks)
            assert (tail.lines, tail.truncated, tail.missing) == (["b", "c"], True, False)
            assert tail.offset == len(b"a\nb\nc\n")  # where a live tail carries on
            assert read.data == b"b\nc\n"
            assert (read.offset, read.shrank) == (len(b"a\nb\nc\n"), False)
            assert unknown.missing

        run_world(tmp_path, body)


class TestRestart:
    """A restarted agent picks up what its journal says was running."""

    def test_restart_reports_finished_from_status_file(self, tmp_path: Path) -> None:
        """Report an attempt that ended while the agent was down, from its status note."""

        async def body(world: World) -> None:
            """Stop the agent mid-job, let the job end, and start a new agent."""
            await world.scheduler.work(shell(_gated(tmp_path)))
            started = await world.scheduler.wait_for(STARTED_TYPE)
            await world.agent.stop()
            (tmp_path / GATE).touch()
            status = tmp_path / "state" / "status" / f"{RUN_ID}.1.json"
            await until(status.exists)
            await world.restart_agent()
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.exit_code == 0
            assert lines(Path(parse_started(started.payload)[1])) == ["done"]

        run_world(tmp_path, body)

    def test_restart_adopts_live_minder(self, tmp_path: Path) -> None:
        """Adopt a minder that outlived the agent and report its ending."""

        async def body(world: World) -> None:
            """Restart the agent while the job runs, then let the job end."""
            await world.scheduler.work(shell(_gated(tmp_path)))
            await world.scheduler.wait_for(STARTED_TYPE)
            await world.restart_agent()
            hello = await world.scheduler.wait_for(HELLO_TYPE, SECOND)
            assert parse_hello(hello.payload)[2] == {(RUN_ID, 1)}
            (tmp_path / GATE).touch()
            status = tmp_path / "state" / "status" / f"{RUN_ID}.1.json"
            await until(status.exists)
            await world.clock.advance(timedelta(seconds=ADOPT_POLL))
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.exit_code == 0

        run_world(tmp_path, body)

    def test_restart_minder_gone(self, tmp_path: Path) -> None:
        """Report an accepted attempt with no minder and no note as a start error."""
        journal = journal_of(tmp_path)
        stem = tmp_path / "logs" / f"{RUN_ID}.1"
        status = tmp_path / "state" / "status" / f"{RUN_ID}.1.json"
        out, err = stem.with_suffix(".out"), stem.with_suffix(".err")
        journal.accept(Entry(RUN_ID, 1, RUNNING, None, out, err, status))
        journal.close()

        async def body(world: World) -> None:
            """Read the report the new agent sends."""
            _, result = parse_finished((await world.scheduler.wait_for(FINISHED_TYPE)).payload)
            assert result.start_error == MINDER_GONE

        run_world(tmp_path, body)


def test_secret_never_in_repr(tmp_path: Path) -> None:
    """Keep the secret out of the agent's repr."""
    agent = make_agent(tmp_path, 1, now_clock())
    assert SECRET not in repr(agent)
    assert NAME in repr(agent)
