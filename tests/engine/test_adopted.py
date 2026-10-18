"""Test the helpers that watch an adopted minder: pid checks, start times and pid files."""

import contextlib
import logging
import os
import signal
import subprocess
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pynet.loop
import pytest
from recovery_helpers import dead_pid

from pyscheduler.engine import adopted, launcher
from pyscheduler.engine.adopted import (
    ADOPT_START_TOLERANCE,
    AdoptedHandle,
    PidNote,
    minder_alive,
    parse_etime,
    pid_alive,
    process_start,
    read_pid,
)
from pyscheduler.engine.clock import FakeClock

START = datetime(2026, 10, 19, tzinfo=UTC)
POLL = 2.0
ANSWER = 42
HOUR = timedelta(hours=1)
SLOW_PS = 1.0
NAP = 0.05


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

            handle.terminate()  # a gone minder is never signalled
            await pynet.loop.sleep(NAP)

        pynet.loop.run(main())
        assert handle.lost
        assert handle.pid > 0

    def test_never_signals_a_stranger(self, tmp_path: Path, orphan: int) -> None:
        """Leave a live pid alone when its start time says it is not our minder."""
        clock = FakeClock(START)
        handle = AdoptedHandle(clock, orphan, START, tmp_path / "s.json", poll=POLL)

        async def main() -> None:
            """Ask for a kill and wait for the handle to give up on the attempt."""
            handle.kill()
            await handle.wait()

        pynet.loop.run(main())
        assert pid_alive(orphan)

    def test_slow_ps_never_blocks_the_loop(
        self, tmp_path: Path, orphan: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Check identity on a worker thread: a slow ``ps`` does not hold up timers."""
        real = adopted.process_start

        def slow(pid: int) -> datetime | None:
            """Answer like ``ps``, a second late."""
            time.sleep(SLOW_PS)
            return real(pid)

        monkeypatch.setattr(adopted, "process_start", slow)
        sent: list[tuple[int, int]] = []

        def record(pid: int, sig: int) -> None:
            """Note the signal instead of sending it."""
            sent.append((pid, sig))

        monkeypatch.setattr(os, "killpg", record)
        handle = AdoptedHandle(
            FakeClock(START), orphan, datetime.now(UTC), tmp_path / "s.json", poll=POLL
        )
        late: list[float] = []

        async def main() -> None:
            """Kill, and time a short nap taken meanwhile."""
            handle.terminate()
            handle.kill()
            began = time.monotonic()
            await pynet.loop.sleep(NAP)
            late.append(time.monotonic() - began - NAP)
            await handle.wait()

        pynet.loop.run(main())
        assert late[0] < SLOW_PS / 2
        assert sent == [(orphan, signal.SIGTERM), (orphan, signal.SIGKILL)]  # in order


class TestElapsedTime:
    """``ps -o etime=`` gives ``[[dd-]hh:]mm:ss``, with no time zone."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("00:05", timedelta(seconds=5)),
            ("  12:34\n", timedelta(minutes=12, seconds=34)),
            ("01:02:03", timedelta(hours=1, minutes=2, seconds=3)),
            ("2-01:02:03", timedelta(days=2, hours=1, minutes=2, seconds=3)),
        ],
    )
    def test_parse_etime(self, text: str, expected: timedelta) -> None:
        """Read every layout ``ps`` uses."""
        assert parse_etime(text) == expected

    @pytest.mark.parametrize("text", ["5", "1:2:3:4", "x-01:02:03", "2-02:03", "ab:cd", ""])
    def test_bad_etime(self, text: str) -> None:
        """Refuse anything else."""
        with pytest.raises(ValueError, match="bad elapsed time"):
            parse_etime(text)

    def test_fall_back_hour(self, orphan: int, monkeypatch: pytest.MonkeyPatch) -> None:
        """Match a minder started in the repeated hour of a DST change (no local time used)."""
        # ``ps`` runs in a zone with a fall-back hour (01:30 EDT and 01:30
        # EST on 1 Nov 2026 are an hour apart). Only the elapsed time is
        # read, so the local clock never has to be guessed.
        monkeypatch.setenv("TZ", "America/New_York")
        assert minder_alive(orphan, datetime.now(UTC))

    def test_live_launched_minder_is_not_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never ``waitpid`` a minder PyNet's child watcher still owns."""
        child = subprocess.Popen(["true"])  # noqa: S607 -- a harmless system command
        monkeypatch.setattr(launcher, "_LIVE_MINDERS", {child.pid})
        time.sleep(NAP)
        assert pid_alive(child.pid)  # still a zombie: its exit status was left alone
        assert child.wait() == 0
