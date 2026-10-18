"""Test the small helpers that watch an adopted minder: pid checks and pid files."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop
import pytest
from recovery_helpers import dead_pid

from pyscheduler.engine.adopted import AdoptedHandle, pid_alive, read_pid
from pyscheduler.engine.clock import FakeClock

START = datetime(2026, 10, 19, tzinfo=UTC)
POLL = 2.0


class TestPidAlive:
    """``pid_alive`` is a plain ``os.kill(pid, 0)`` check."""

    def test_own_pid_is_alive(self) -> None:
        """Find this test process alive."""
        assert pid_alive(os.getpid())

    def test_dead_and_silly_pids(self) -> None:
        """Find a reaped pid, 0 and negative pids dead (0 would mean our own group)."""
        assert not pid_alive(dead_pid())
        assert not pid_alive(0)
        assert not pid_alive(-1)

    def test_someone_elses_pid_is_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Count a process we may not signal as alive."""

        def refuse(_pid: int, _sig: int) -> None:
            """Act like a process owned by another user."""
            raise PermissionError

        monkeypatch.setattr(os, "kill", refuse)
        assert pid_alive(1)


class TestReadPid:
    """The minder's ``.pid`` file is read defensively."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [('{"pid": 42}', 42), ("[1]", None), ('{"pid": true}', None), ("not json", None)],
    )
    def test_read_pid(self, tmp_path: Path, text: str, expected: int | None) -> None:
        """Return the pid only from a well-formed file."""
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
        handle = AdoptedHandle(FakeClock(START), dead_pid(), tmp_path / "s.json", poll=POLL)

        async def main() -> None:
            """Wait for the (already gone) attempt."""
            result = await handle.wait()
            assert result.exit_code is None
            assert result.start_error is None

        pynet.loop.run(main())
        assert handle.lost
        assert handle.pid > 0
        handle.terminate()  # an empty group is not an error
