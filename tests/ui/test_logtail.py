"""Test the live log tail: ``read_from`` and ``GET /events/log/<run_id>``."""

import json
from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from api_helpers import (
    BAD_REQUEST,
    DEV,
    NOT_FOUND,
    ApiHarness,
    MakeApi,
    body_of,
    log_in,
    users_yaml,
)
from pyweb.sse import ServerEvent
from ui_helpers import HOSTILE, next_event, open_stream

from pyscheduler.testing import FakeResult
from pyscheduler.ui import logtail
from pyscheduler.ui.logtail import LOG_CHUNK, LOG_POLL, read_from

QUICK_POLL = 0.01
"""A test's poll interval (real seconds), instead of ``LOG_POLL``."""
LIMIT = 4
SMALL_CHUNK = 8
TAIL_LINES = 200
OLD_LINES = 250
ADHOC = "adhoc.2026-10-19.1"
LONG = timedelta(minutes=10)
SEE_OTHER = 303
UNAUTHORISED = 401


def _lines(event: ServerEvent) -> list[str]:
    """Return a ``lines`` event's list of lines."""
    assert event.event == "lines"
    found: list[str] = json.loads(event.data)
    return found


async def _started(h: ApiHarness, job: str = "adhoc") -> Path:
    """Trigger ``job`` (it runs for ``LONG``) and return its attempt's stdout path."""
    h.launcher.plan(job, FakeResult(duration=LONG))
    made = await h.client.post(f"/api/jobs/{job}/trigger", headers=DEV)
    run_id = body_of(made)["run"]["run_id"]
    await h.engine.settle()
    (attempt,) = await pynet.loop.run_in_thread(h.store.attempts_for, run_id)
    path = Path(attempt.stdout_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestReadFrom:
    """``read_from`` reads what was added since an offset."""

    def test_read_from(self, tmp_path: Path) -> None:
        """Appended bytes come from the offset; the limit holds; a shrunk file restarts at 0."""
        assert (LOG_POLL, LOG_CHUNK) == (1.0, 65536)
        path = tmp_path / "out.log"
        assert read_from(path, 0, LIMIT) == (b"", 0, False)  # not there yet
        path.write_bytes(b"abcdef")
        assert read_from(path, 0, LIMIT) == (b"abcd", LIMIT, False)
        assert read_from(path, LIMIT, LIMIT) == (b"ef", len(b"abcdef"), False)
        with path.open("ab") as handle:
            handle.write(b"gh")
        assert read_from(path, len(b"abcdef"), LIMIT) == (b"gh", len(b"abcdefgh"), False)
        assert read_from(path, len(b"abcdefgh"), LIMIT) == (b"", len(b"abcdefgh"), False)
        path.write_bytes(b"xy")  # rotated: shorter than where we were
        assert read_from(path, len(b"abcdefgh"), LIMIT) == (b"xy", len(b"xy"), True)


class TestLogTail:
    """The tail sends the last lines, then new whole lines, then ``end``."""

    def test_log_tail_streams_new_lines(
        self, api: MakeApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First the tail; then appended lines, partial ones held back; ``end`` after the end."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)

        async def body() -> None:
            """Write, append, rotate and end the attempt while reading the stream."""
            path = await _started(h)
            old = [f"old {n}" for n in range(OLD_LINES)]
            path.write_bytes(("\n".join(old) + "\nhalf").encode())
            stream = await open_stream(h, f"/events/log/{ADHOC}", DEV)
            first = _lines(await next_event(stream))
            assert first == old[-TAIL_LINES:]  # the unfinished "half" waits
            with path.open("ab") as handle:
                handle.write(b" done\nnext\r\npar")
            assert _lines(await next_event(stream)) == ["half done", "next"]
            with path.open("ab") as handle:
                handle.write(b"tial\n")
            assert _lines(await next_event(stream)) == ["partial"]
            path.write_bytes(b"fresh\n")  # rotated
            reset = await next_event(stream)
            assert (reset.event, reset.data) == ("reset", "{}")
            assert _lines(await next_event(stream)) == ["fresh"]
            with path.open("ab") as handle:
                handle.write(b"last words")  # no newline: sent once the attempt ends
            await h.advance_to(h.clock.now() + LONG)
            assert _lines(await next_event(stream)) == ["last words"]
            end = await next_event(stream)
            assert (end.event, end.data) == ("end", "{}")
            with pytest.raises(StopAsyncIteration):
                await next_event(stream)
            await stream.aclose()

        h.run(body)

    def test_log_tail_of_ended_attempt(self, api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
        """An attempt that already ended gives its tail, then ``end``; a missing file is empty."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)

        async def body() -> None:
            """Let the attempt end with no log file, then tail it."""
            await _started(h)
            await h.advance_to(h.clock.now() + LONG)
            events = await h.client.stream(f"/events/log/{ADHOC}", count=10, headers=DEV)
            assert [(e.event, e.data) for e in events] == [("lines", "[]"), ("end", "{}")]

        h.run(body)

    def test_log_tail_unfinished_lines(self, api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
        """A file that is one unfinished line waits for the end; one over a chunk is sent."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)

        async def body() -> None:
            """Tail a file with no newline, then one whose last line is longer than a chunk."""
            path = await _started(h)
            path.write_bytes(b"half")
            stream = await open_stream(h, f"/events/log/{ADHOC}", DEV)
            assert _lines(await next_event(stream)) == []
            await h.advance_to(h.clock.now() + LONG)
            assert _lines(await next_event(stream)) == ["half"]
            assert (await next_event(stream)).event == "end"
            await stream.aclose()
            monkeypatch.setattr(logtail, "LOG_CHUNK", SMALL_CHUNK)
            long_line = "y" * (SMALL_CHUNK * 2)
            path.write_text(f"x\n{long_line}", encoding="utf-8")
            events = await h.client.stream(f"/events/log/{ADHOC}", count=10, headers=DEV)
            assert [(e.event, e.data) for e in events] == [
                ("lines", json.dumps(["x", long_line])),
                ("end", "{}"),
            ]

        h.run(body)

    def test_log_tail_closes_cleanly(self, api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
        """Closing the stream stops the polling and forgets the attempt listener."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)

        async def body() -> None:
            """Open a tail, read the first event, close it while it polls."""
            await _started(h)
            listeners = len(h.engine._attempt_listeners)
            stream = await open_stream(h, f"/events/log/{ADHOC}", DEV)
            await next_event(stream)
            assert len(h.engine._attempt_listeners) == listeners + 1
            pending = pynet.loop.spawn(next_event(stream))
            await pynet.loop.sleep(QUICK_POLL * 3)
            pending.cancel()  # as the server does when the browser leaves
            await pynet.loop.gather(pending, return_exceptions=True)
            await stream.aclose()
            assert len(h.engine._attempt_listeners) == listeners

        h.run(body)

    def test_log_tail_escapes_nothing_but_is_text(
        self, api: MakeApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lines travel as JSON strings, unescaped (app.js inserts them with ``textContent``)."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)

        async def body() -> None:
            """Log hostile text and bad bytes, then tail it."""
            path = await _started(h)
            path.write_bytes(HOSTILE.encode() + b"\nbad \xff\n")
            (event,) = await h.client.stream(f"/events/log/{ADHOC}", count=1, headers=DEV)
            assert event.data == json.dumps([HOSTILE, "bad �"])
            assert json.loads(event.data) == [HOSTILE, "bad �"]
            assert str(path) not in event.data

        h.run(body)

    def test_log_tail_unreadable_file(
        self, api: MakeApi, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A log that is there but cannot be read ends the stream with ``end`` and one warning."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(tokens=False)

        async def body() -> None:
            """Make the log path a folder, then tail it; then break it after the first look."""
            path = await _started(h)
            path.mkdir()
            events = await h.client.stream(f"/events/log/{ADHOC}", count=10, headers=DEV)
            assert [(e.event, e.data) for e in events] == [("end", "{}")]
            path.rmdir()
            stream = await open_stream(h, f"/events/log/{ADHOC}", DEV)
            assert _lines(await next_event(stream)) == []
            path.mkdir()  # now each poll fails to open it
            assert (await next_event(stream)).event == "end"
            with pytest.raises(StopAsyncIteration):
                await next_event(stream)

        h.run(body)
        warnings = [r for r in caplog.records if "log tail stopped" in r.getMessage()]
        assert len(warnings) == len(("first look", "poll"))

    def test_viewer_streams_a_log(self, api: MakeApi) -> None:
        """A logged-in viewer receives the first ``lines`` of a running attempt's log."""
        h = api(tokens=False, users=users_yaml(vic="viewer"))

        async def body() -> None:
            """Start ``adhoc`` from the engine, write its log, and tail it as ``vic``."""
            h.launcher.plan("adhoc", FakeResult(duration=LONG))
            await h.engine.trigger("adhoc", actor="test")
            await h.engine.settle()
            (attempt,) = await pynet.loop.run_in_thread(h.store.attempts_for, ADHOC)
            path = Path(attempt.stdout_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("hello\n", encoding="utf-8")
            log_in(h, "vic")
            (event,) = await h.client.stream(f"/events/log/{ADHOC}", count=1)
            assert _lines(event) == ["hello"]

        h.run(body)

    def test_log_tail_errors(self, api: MakeApi) -> None:
        """Bad queries, unknown runs, no attempt yet and merged stderr answer as the API does."""
        h = api(
            """
            jobs:
              adhoc:
                command: run-adhoc
                schedule: manual
                stderr: stdout
              later:
                command: run-later
                depends_on: [adhoc]
            """,
            tokens=False,
        )

        async def body() -> None:
            """Ask for logs that cannot be tailed."""
            await _started(h)
            waiting = await h.client.post("/api/jobs/later/trigger", headers=DEV)
            later = body_of(waiting)["run"]["run_id"]
            cases = {
                f"/events/log/{ADHOC}?stream=both": BAD_REQUEST,
                f"/events/log/{ADHOC}?attempt=x": BAD_REQUEST,
                f"/events/log/{ADHOC}?stream=stderr": NOT_FOUND,
                f"/events/log/{ADHOC}?attempt=2": NOT_FOUND,
                "/events/log/nope.2026-10-19.1": NOT_FOUND,
                f"/events/log/{later}": NOT_FOUND,
            }
            for url, status in cases.items():
                response = await h.client.get(url, headers=DEV)
                assert response.status == status, url
                assert "error" in body_of(response), url

        h.run(body)

    def test_log_tail_needs_login(self, api: MakeApi) -> None:
        """Logged out is refused; any logged-in viewer may tail a log."""
        h = api(tokens=False, users=users_yaml(vic="viewer"))

        async def body() -> None:
            """Ask without a session, then as a viewer."""
            url = f"/events/log/{ADHOC}"
            assert (await h.client.get(url)).status == UNAUTHORISED
            page = await h.client.get(url, headers={"Accept": "text/html"})
            assert page.status == SEE_OTHER
            log_in(h, "vic")
            assert (await h.client.get(url)).status == NOT_FOUND  # past the door: no such run

        h.run(body)
