"""Test the helpers that watch an adopted minder: pid checks, start times and pid files."""

import contextlib
import logging
import os
import signal
import subprocess
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pynet.loop
import pytest
from recovery_helpers import dead_pid

from pyscheduler.engine import adopted
from pyscheduler.engine.adopted import (
    ADOPT_START_TOLERANCE,
    AdoptedHandle,
    PidNote,
    minder_alive,
    pid_alive,
    process_start,
    read_pid,
)
from pyscheduler.engine.clock import FakeClock

START = datetime(2026, 10, 19, tzinfo=UTC)
POLL = 2.0
ANSWER = 42
HOUR = timedelta(hours=1)


@pytest.fixture
def orphan() -> Generator[int]:
    """Start a ``sleep 30`` that is not our child (its parent shell exits), like a minder.

    Yields its pid; it is killed afterwards.
    """
    shell = subprocess.run(
        ["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(shell.stdout.strip())
    try:
        yield pid
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


class TestPidAlive:
    """``pid_alive`` is the cheap check: our child, or a pid we may signal."""

    def test_own_pid_is_alive(self) -> None:
        """Find this test process alive."""
        assert pid_alive(os.getpid())

    def test_dead_and_silly_pids(self) -> None:
        """Find a reaped pid, 0 and negative pids dead (0 would mean our own group)."""
        assert not pid_alive(dead_pid())
        assert not pid_alive(0)
        assert not pid_alive(-1)

    def test_someone_elses_pid_is_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Count a process we may not signal as gone: a minder always runs as us."""

        def refuse(_pid: int, _sig: int) -> None:
            """Act like a process owned by another user."""
            raise PermissionError

        monkeypatch.setattr(os, "kill", refuse)
        assert not pid_alive(os.getpid())

    def test_own_child_is_reaped(self) -> None:
        """Clear away our own ended child instead of seeing a zombie as alive."""
        child = subprocess.Popen(["true"])  # noqa: S607 -- a harmless system command
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        while pid_alive(child.pid):
            assert datetime.now(UTC) < deadline
        assert not pid_alive(child.pid)


class TestMinderAlive:
    """``minder_alive`` also checks the process started when the minder did."""

    def test_matching_start_time(self, orphan: int) -> None:
        """Accept a process that started when its ``.pid`` file says."""
        began = process_start(orphan)
        assert began is not None
        assert abs(began - datetime.now(UTC)) < ADOPT_START_TOLERANCE
        assert minder_alive(orphan, datetime.now(UTC))

    def test_other_start_time_is_a_stranger(self, orphan: int) -> None:
        """Refuse a live pid whose start time does not match (a reused pid)."""
        assert not minder_alive(orphan, datetime.now(UTC) - HOUR)

    def test_no_such_process(self) -> None:
        """Report no start time for a pid that is gone."""
        assert process_start(dead_pid()) is None
        assert not minder_alive(dead_pid(), datetime.now(UTC))
        assert not minder_alive(0, datetime.now(UTC))

    def test_ps_missing_trusts_pid(
        self,
        orphan: int,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fall back to the pid check, with a warning, when ``ps`` cannot run."""

        def no_ps(*_args: object, **_kwargs: object) -> None:
            """Act like a system without ``ps``."""
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", no_ps)
        with caplog.at_level(logging.WARNING):
            assert minder_alive(orphan, datetime.now(UTC) - HOUR)
        assert "cannot read the start time" in caplog.text

    @pytest.mark.parametrize("output", ["garbage here", "timeout"])
    def test_bad_ps_answer_trusts_pid(
        self, orphan: int, monkeypatch: pytest.MonkeyPatch, output: str
    ) -> None:
        """Treat an unreadable or slow ``ps`` like a missing one."""

        def odd_ps(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Answer nonsense, or time out."""
            if output == "timeout":
                command = "ps"
                raise subprocess.TimeoutExpired(command, adopted.PS_TIMEOUT)
            return subprocess.CompletedProcess(["ps"], 0, output, "")

        monkeypatch.setattr(subprocess, "run", odd_ps)
        assert minder_alive(orphan, datetime.now(UTC) - HOUR)


class TestReadPid:
    """The minder's ``.pid`` file is read defensively."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"pid": 42, "started_at": "2026-10-19T00:00:00Z"}', PidNote(ANSWER, START)),
            ('{"pid": 42}', PidNote(ANSWER, None)),
            ('{"pid": 42, "started_at": "2026-10-19T00:00:00"}', PidNote(ANSWER, None)),
            ('{"pid": 42, "started_at": "soon"}', PidNote(ANSWER, None)),
            ("[1]", None),
            ('{"pid": true}', None),
            ("not json", None),
        ],
    )
    def test_read_pid(self, tmp_path: Path, text: str, expected: PidNote | None) -> None:
        """Return the pid (and an aware start time) only from a well-formed file."""
        status = tmp_path / "run.1.json"
        status.with_name("run.1.json.pid").write_text(text, encoding="utf-8")
        assert read_pid(status) == expected

    def test_missing_file(self, tmp_path: Path) -> None:
        """Return ``None`` when there is no pid file."""
        assert read_pid(tmp_path / "nothing.json") is None


class TestAdoptedHandle:
    """An adopted handle ends when its note appears or its process is gone."""

    def test_gone_without_note_is_lost(self, tmp_path: Path) -> None:
        """Report a vanished minder with no note as lost."""
        clock = FakeClock(START)
        handle = AdoptedHandle(clock, dead_pid(), START, tmp_path / "s.json", poll=POLL)

        async def main() -> None:
            """Wait for the (already gone) attempt."""
            result = await handle.wait()
            assert result.exit_code is None
            assert result.start_error is None

        pynet.loop.run(main())
        assert handle.lost
        assert handle.pid > 0
        handle.terminate()  # a gone minder is never signalled

    def test_never_signals_a_stranger(self, tmp_path: Path, orphan: int) -> None:
        """Leave a live pid alone when its start time says it is not our minder."""
        clock = FakeClock(START)
        handle = AdoptedHandle(clock, orphan, START, tmp_path / "s.json", poll=POLL)
        handle.kill()
        assert pid_alive(orphan)
