"""Tests for the minder launcher and reading the minder's status note."""

import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop
import pytest

from pyscheduler.engine.launcher import AttemptResult, LaunchSpec, MinderLauncher, read_status
from pyscheduler.minder import write_status

EXIT_FOUR = 4
EXIT_THREE = 3
WAIT_SECONDS = 5.0
GONE_SECONDS = 2.0
POLL = 0.01
SETTLE = 0.2
DEEP = 100_000


def make_spec(tmp_path: Path, argv: list[str], *, stderr: bool = True) -> LaunchSpec:
    """Build a launch spec writing everything under ``tmp_path``."""
    return LaunchSpec(
        run_id="job.2026-10-19.1",
        attempt=1,
        argv=tuple(argv),
        cwd=str(tmp_path),
        env=dict(os.environ),
        stdout_path=tmp_path / "logs" / "a.out",
        stderr_path=tmp_path / "logs" / "a.err" if stderr else None,
        status_path=tmp_path / "status" / "a.json",
    )


async def wait_for_file(path: Path) -> None:
    """Poll on the loop until ``path`` exists."""
    async with pynet.loop.timeout(WAIT_SECONDS):
        while not path.exists():
            await pynet.loop.sleep(POLL)


def group_alive(pgid: int) -> bool:
    """Return whether any process is left in group ``pgid``."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def fake_python(tmp_path: Path, body: str) -> str:
    """Write an executable shell script to stand in for python; return its path."""
    script = tmp_path / "fakepython"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


class TestAttemptResult:
    """An attempt succeeds only with exit 0, no signal and no start error."""

    def test_succeeded(self) -> None:
        """Report success for a clean exit and failure otherwise."""
        now = datetime(2026, 10, 19, tzinfo=UTC)
        assert AttemptResult(0, None, now, now, None).succeeded
        assert not AttemptResult(1, None, now, now, None).succeeded
        assert not AttemptResult(None, signal.SIGKILL, now, now, None).succeeded
        assert not AttemptResult(None, None, now, now, "boom").succeeded


class TestReadStatus:
    """``read_status`` turns the minder's note into an ``AttemptResult``."""

    def test_missing_is_none(self, tmp_path: Path) -> None:
        """Return None when there is no note."""
        assert read_status(tmp_path / "nope.json") is None

    def test_reads_note(self, tmp_path: Path) -> None:
        """Parse a note with ``Z`` timestamps into aware UTC datetimes."""
        path = tmp_path / "s.json"
        write_status(
            path,
            {
                "exit_code": EXIT_THREE,
                "signal": None,
                "started_at": "2026-10-19T09:00:00.000000Z",
                "ended_at": "2026-10-19T09:00:04.250000Z",
                "start_error": None,
            },
        )
        result = read_status(path)
        assert result == AttemptResult(
            exit_code=EXIT_THREE,
            signal=None,
            started_at=datetime(2026, 10, 19, 9, tzinfo=UTC),
            ended_at=datetime(2026, 10, 19, 9, 0, 4, 250000, tzinfo=UTC),
            start_error=None,
        )

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "[1, 2]",
            '{"exit_code": "x", "signal": null, "ended_at": "2026-10-19T09:00:00Z"}',
            '{"exit_code": 0, "signal": null, "started_at": null, "ended_at": "soon"}',
            '{"exit_code": 0, "signal": null, "started_at": null}',
            '{"exit_code": true, "signal": null, "ended_at": "2026-10-19T09:00:00Z"}',
            '{"exit_code": 0, "signal": null, "ended_at": "2026-10-19T09:00:00"}',
            pytest.param("[" * DEEP + "]" * DEEP, id="too-deep"),
        ],
    )
    def test_corrupt_note(self, tmp_path: Path, text: str) -> None:
        """Report a corrupt note as a start error instead of raising."""
        path = tmp_path / "s.json"
        path.write_text(text)
        result = read_status(path)
        assert result is not None
        assert result.start_error is not None
        assert result.start_error.startswith("corrupt status file:")
        assert not result.succeeded


class TestMinderLauncher:
    """The real launcher runs each attempt under the minder."""

    def test_minder_launcher_runs_command(self, tmp_path: Path) -> None:
        """Run ``sh -c 'exit 4'`` and get exit code 4 plus both log files."""
        spec = make_spec(tmp_path, ["sh", "-c", "echo out; echo err >&2; exit 4"])

        async def main() -> AttemptResult:
            """Launch and wait."""
            handle = await MinderLauncher().launch(spec)
            assert handle.pid is not None
            first = await handle.wait()
            assert await handle.wait() is first
            return first

        result = pynet.loop.run(main())
        assert result.exit_code == EXIT_FOUR
        assert result.signal is None
        assert result.start_error is None
        assert result.started_at is not None
        assert result.started_at <= result.ended_at
        assert spec.stdout_path.read_text() == "out\n"
        assert spec.stderr_path is not None
        assert spec.stderr_path.read_text() == "err\n"

    def test_without_stderr_path_merges_logs(self, tmp_path: Path) -> None:
        """Send stderr to the stdout log when no stderr path is given."""
        spec = make_spec(tmp_path, ["sh", "-c", "echo err >&2"], stderr=False)

        async def main() -> AttemptResult:
            """Launch and wait."""
            return await (await MinderLauncher().launch(spec)).wait()

        assert pynet.loop.run(main()).succeeded
        assert spec.stdout_path.read_text() == "err\n"

    def test_removes_stale_status(self, tmp_path: Path) -> None:
        """Remove an old note and pid file before launching."""
        spec = make_spec(tmp_path, ["true"])
        pid_path = spec.status_path.with_name(f"{spec.status_path.name}.pid")
        write_status(spec.status_path, {"exit_code": 9})
        pid_path.write_text("{}")
        launcher = MinderLauncher(python=fake_python(tmp_path, "exit 0"))

        async def main() -> AttemptResult:
            """Launch a stand-in minder that writes nothing."""
            return await (await launcher.launch(spec)).wait()

        result = pynet.loop.run(main())
        assert not spec.status_path.exists()
        assert not pid_path.exists()
        assert result.exit_code == 0
        assert result.start_error is not None
        assert not result.succeeded

    def test_start_error_from_minder(self, tmp_path: Path) -> None:
        """Report a program that does not exist through the minder's note."""
        spec = make_spec(tmp_path, [str(tmp_path / "no-such-program")])

        async def main() -> AttemptResult:
            """Launch and wait."""
            return await (await MinderLauncher().launch(spec)).wait()

        result = pynet.loop.run(main())
        assert result.exit_code is None
        assert result.start_error is not None

    def test_missing_python_raises(self, tmp_path: Path) -> None:
        """Let an ``OSError`` from starting the minder escape ``launch``."""
        spec = make_spec(tmp_path, ["true"])
        launcher = MinderLauncher(python=str(tmp_path / "no-python"))

        async def main() -> None:
            """Try to launch."""
            await launcher.launch(spec)

        with pytest.raises(FileNotFoundError):
            pynet.loop.run(main())

    def test_minder_killed_without_note(self, tmp_path: Path) -> None:
        """Synthesise a result from the minder's own signal when no note exists."""
        spec = make_spec(tmp_path, ["true"])
        launcher = MinderLauncher(python=fake_python(tmp_path, "kill -9 $$"))

        async def main() -> AttemptResult:
            """Launch a stand-in minder that kills itself."""
            return await (await launcher.launch(spec)).wait()

        result = pynet.loop.run(main())
        assert result.exit_code is None
        assert result.signal == signal.SIGKILL
        assert result.started_at is None
        assert result.start_error is None

    def test_terminate_writes_note(self, tmp_path: Path) -> None:
        """Stop a job with SIGTERM; the minder still writes its note."""
        spec = make_spec(tmp_path, ["sleep", "100"])
        pid_path = spec.status_path.with_name(f"{spec.status_path.name}.pid")

        async def main() -> AttemptResult:
            """Launch, wait until it is running, terminate."""
            handle = await MinderLauncher().launch(spec)
            await wait_for_file(pid_path)
            await pynet.loop.sleep(SETTLE)
            handle.terminate()
            async with pynet.loop.timeout(WAIT_SECONDS):
                result = await handle.wait()
            handle.terminate()  # already finished: harmless
            handle.kill()
            return result

        result = pynet.loop.run(main())
        assert result.signal == signal.SIGTERM
        assert result.started_at is not None
        assert spec.status_path.exists()

    def test_kill_stops_grandchildren(self, tmp_path: Path) -> None:
        """Kill the whole group, background grandchildren included."""
        spec = make_spec(tmp_path, ["sh", "-c", "sleep 100 & sleep 100 & wait"])
        pid_path = spec.status_path.with_name(f"{spec.status_path.name}.pid")

        async def main() -> tuple[int, AttemptResult]:
            """Launch, wait until it is running, kill."""
            handle = await MinderLauncher().launch(spec)
            assert handle.pid is not None
            await wait_for_file(pid_path)
            await pynet.loop.sleep(SETTLE)
            handle.kill()
            async with pynet.loop.timeout(WAIT_SECONDS):
                result = await handle.wait()
            return handle.pid, result

        pgid, result = pynet.loop.run(main())
        assert result.signal == signal.SIGKILL
        deadline = time.monotonic() + GONE_SECONDS
        while group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(POLL)
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)


def test_python_default_is_current_interpreter() -> None:
    """Use the running interpreter unless told otherwise."""
    assert MinderLauncher().python == sys.executable


def test_note_round_trip_json(tmp_path: Path) -> None:
    """Accept a note whose ``started_at`` is null."""
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "exit_code": None,
                "signal": None,
                "started_at": None,
                "ended_at": "2026-10-19T09:00:00Z",
                "start_error": "boom",
            }
        )
    )
    result = read_status(path)
    assert result is not None
    assert result.started_at is None
    assert result.start_error == "boom"
