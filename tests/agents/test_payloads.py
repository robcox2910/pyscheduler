"""Tests for the payload builders and parsers shared by the agent and the hub."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pynet.messaging import JsonValue

from pyscheduler.agents.log_messages import (
    MAX_READ_BYTES,
    MAX_TAIL_LINES,
    LogReply,
    LogRequest,
    log_reply_payload,
    log_request_payload,
    parse_log_reply,
    parse_log_request,
)
from pyscheduler.agents.messages import (
    WorkOrder,
    finished_payload,
    heartbeat_payload,
    hello_payload,
    key_payload,
    parse_finished,
    parse_heartbeat,
    parse_hello,
    parse_key,
    parse_started,
    parse_work,
    started_payload,
    work_payload,
)
from pyscheduler.agents.signing import Rejected
from pyscheduler.agents.worker_files import attempt_files
from pyscheduler.engine.status_note import AttemptResult

RUN_ID = "nightly.2026-10-20.1"
WHEN = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ORDER = WorkOrder(
    RUN_ID, 1, "nightly", "2026-10-20", ("echo", "hi"), "/srv/jobs", {"A": "secret-b"}
)
REQUEST_ID = "ab" * 8
MAX_RUNNING = 3


class TestRoundTrips:
    """Every builder's payload parses back to what went in."""

    def test_work(self) -> None:
        """Keep every field of a work order; hide argv and env from the repr."""
        assert parse_work(work_payload(ORDER)) == ORDER
        assert "secret-b" not in repr(ORDER)
        assert "echo" not in repr(ORDER)
        assert ORDER.key == (RUN_ID, 1)

    def test_reports(self) -> None:
        """Round-trip hello, heartbeat, started and finished."""
        running = {(RUN_ID, 1)}
        hello = hello_payload({"gpu", "linux"}, MAX_RUNNING, running)
        assert parse_hello(hello) == (frozenset({"gpu", "linux"}), MAX_RUNNING, running)
        beat = heartbeat_payload(running, tags={"gpu"}, max_running=MAX_RUNNING)
        assert parse_heartbeat(beat) == (frozenset({"gpu"}), MAX_RUNNING, running)
        assert parse_heartbeat(heartbeat_payload(running)) == (None, None, running)
        started = started_payload(RUN_ID, 1, "/l/a.out", "/l/a.err")
        assert parse_started(started) == ((RUN_ID, 1), "/l/a.out", "/l/a.err")
        for result in (
            AttemptResult(0, None, WHEN, WHEN, None),
            AttemptResult(None, 15, None, WHEN, "boom"),
        ):
            assert parse_finished(finished_payload(RUN_ID, 1, result)) == ((RUN_ID, 1), result)

    def test_log(self) -> None:
        """Round-trip both request modes and both reply kinds."""
        tail = LogRequest(REQUEST_ID, RUN_ID, 1, "stdout", "tail", count=5)
        read = LogRequest(REQUEST_ID, RUN_ID, 1, "stderr", "read", offset=3, limit=9)
        for request in (tail, read):
            assert parse_log_request(log_request_payload(request)) == request
        for reply in (
            LogReply(REQUEST_ID, lines=["a"], truncated=True),
            LogReply(REQUEST_ID, lines=["a"], offset=2),
            LogReply(REQUEST_ID, data=b"\x00hi", offset=7, shrank=True),
            LogReply(REQUEST_ID, missing=True),
        ):
            assert parse_log_reply(log_reply_payload(reply)) == reply

    def test_log_sizes_capped(self) -> None:
        """Cap a huge tail count and read limit."""
        tail = LogRequest(REQUEST_ID, RUN_ID, 1, "stdout", "tail", count=10**9)
        read = LogRequest(REQUEST_ID, RUN_ID, 1, "stdout", "read", offset=0, limit=10**12)
        assert parse_log_request(log_request_payload(tail)).count == MAX_TAIL_LINES
        assert parse_log_request(log_request_payload(read)).limit == MAX_READ_BYTES


def _work(**changes: JsonValue) -> dict[str, JsonValue]:
    """Return a work payload with ``changes`` applied."""
    return work_payload(ORDER) | changes


class TestRejects:
    """Malformed payloads raise ``Rejected``."""

    @pytest.mark.parametrize(
        "payload",
        [
            _work(run_id="../x"),
            _work(run_id=7),
            _work(attempt=0),
            _work(attempt=True),
            _work(job="a/b"),
            _work(job="other"),
            _work(run_date="2026-10-21"),
            _work(run_id="/var/x.2026-10-20.1", job="x"),
            _work(run_id="a/b.2026-10-20.1", job="b"),
            _work(run_id="...2026-10-20.1"),
            _work(run_date="2026-13-01"),
            _work(argv=[]),
            _work(argv="echo"),
            _work(argv=["echo", 1]),
            _work(env={"A": 1}),
            _work(env=[]),
            _work(cwd=5),
        ],
    )
    def test_bad_work(self, payload: dict[str, JsonValue]) -> None:
        """Refuse a work payload with a bad field."""
        with pytest.raises(Rejected, match="malformed payload"):
            parse_work(payload)

    @pytest.mark.parametrize(
        ("parse", "payload"),
        [
            (parse_hello, {"tags": "gpu", "max_running": 1, "running": []}),
            (parse_hello, {"tags": ["bad tag"], "max_running": 1, "running": []}),
            (parse_hello, {"tags": [], "max_running": -1, "running": []}),
            (parse_heartbeat, {"running": [[RUN_ID]]}),
            (parse_heartbeat, {"running": "x"}),
            (parse_heartbeat, {"running": [[RUN_ID, "1"]]}),
            (parse_heartbeat, {"running": [], "tags": ["bad tag"], "max_running": 1}),
            (parse_heartbeat, {"running": [], "tags": ["gpu"], "max_running": True}),
            (parse_key, {"run_id": RUN_ID}),
            (parse_key, {"run_id": "/var/x.2026-10-20.1", "attempt": 1}),
            (parse_started, key_payload("a/b.2026-10-20.1", 1)),
            (parse_finished, key_payload(RUN_ID, 1) | {"ended_at": "yesterday"}),
            (parse_finished, key_payload(RUN_ID, 1) | {"ended_at": "2026-10-20T12:00:00"}),
            (parse_finished, key_payload(RUN_ID, 1) | {"ended_at": 5}),
            (parse_finished, key_payload(RUN_ID, 1) | {"exit_code": "0"}),
            (parse_log_request, key_payload(RUN_ID, 1) | {"request_id": "r", "stream": "x"}),
            (
                parse_log_request,
                key_payload(RUN_ID, 1) | {"request_id": "r", "stream": "stdout", "mode": "tail"},
            ),
            (parse_log_reply, {"request_id": "r", "lines": [1]}),
            (parse_log_reply, {"request_id": "r", "data_b64": "!!", "offset": 0}),
            (parse_log_reply, {"request_id": "r", "missing": "yes"}),
        ],
    )
    def test_bad_payloads(self, parse: object, payload: dict[str, JsonValue]) -> None:
        """Refuse a malformed report or log payload."""
        assert callable(parse)
        with pytest.raises(Rejected, match="malformed payload"):
            parse(payload)


class TestAttemptFiles:
    """An attempt's files always stay inside the agent's folders."""

    def test_paths(self, tmp_path: Path) -> None:
        """Put logs under the job and date, and the note under ``status``."""
        out, err, status = attempt_files(ORDER, tmp_path / "logs", tmp_path / "state")
        assert out == tmp_path / "logs" / "nightly" / "2026-10-20" / f"{RUN_ID}.1.out"
        assert err.name == f"{RUN_ID}.1.err"
        assert status == tmp_path / "state" / "status" / f"{RUN_ID}.1.json"

    @pytest.mark.parametrize(
        ("run_id", "job"), [("/var/x.2026-10-20.1", "x"), ("../../x.2026-10-20.1", "nightly")]
    )
    def test_refuses_escape(self, tmp_path: Path, run_id: str, job: str) -> None:
        """Raise for an order (built without the payload checks) whose files would escape."""
        bad = replace(ORDER, run_id=run_id, job=job)
        with pytest.raises(ValueError, match="leave"):
            attempt_files(bad, tmp_path / "logs", tmp_path / "state")
