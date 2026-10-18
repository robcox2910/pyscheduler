"""Tests for the minder: the small program that babysits one job attempt."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from pyscheduler.minder import _Forwarder, main, write_status

EXIT_THREE = 3
EXIT_SEVEN = 7
START_ERROR_EXIT = 127
SIGKILL_EXIT = 128 + signal.SIGKILL
PID_WAIT_SECONDS = 5.0
RUN_TIMEOUT_SECONDS = 8.0


def run_minder(
    tmp_path: Path, argv: list[str], *, stderr: bool = True, cwd: Path | None = None
) -> int:
    """Run the minder as a real subprocess and return its exit code."""
    cmd = [sys.executable, "-I", "-m", "pyscheduler.minder", "--status", str(tmp_path / "s.json")]
    cmd += ["--stdout", str(tmp_path / "logs" / "a.out")]
    if stderr:
        cmd += ["--stderr", str(tmp_path / "logs" / "a.err")]
    cmd += ["--", *argv]
    return subprocess.run(cmd, check=False, timeout=RUN_TIMEOUT_SECONDS, cwd=cwd).returncode  # noqa: S603


def read_json(path: Path) -> dict[str, object]:
    """Read a JSON object from a file."""
    data: dict[str, object] = json.loads(path.read_text())
    return data


def wait_for(path: Path) -> None:
    """Poll until a file exists, failing after a deadline."""
    deadline = time.monotonic() + PID_WAIT_SECONDS
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{path} never appeared")
        time.sleep(0.01)


class TestMinderProcess:
    """The minder run as `python -m pyscheduler.minder`."""

    def test_exit_code_and_logs(self, tmp_path: Path) -> None:
        """The exit code lands in the status, and stdout/stderr in their own files."""
        code = run_minder(tmp_path, ["sh", "-c", "echo out; echo err >&2; exit 3"])
        assert code == EXIT_THREE
        status = read_json(tmp_path / "s.json")
        assert status["exit_code"] == EXIT_THREE
        assert status["signal"] is None
        assert status["start_error"] is None
        assert (tmp_path / "logs" / "a.out").read_text() == "out\n"
        assert (tmp_path / "logs" / "a.err").read_text() == "err\n"
        pid = read_json(tmp_path / "s.json.pid")
        assert isinstance(pid["pid"], int)
        assert str(pid["started_at"]).endswith("Z")
        for key in ("started_at", "ended_at"):
            stamp = str(status[key])
            assert stamp.endswith("Z")
            datetime.fromisoformat(stamp)

    def test_merged_stderr(self, tmp_path: Path) -> None:
        """Without --stderr, both streams go into the stdout file."""
        run_minder(tmp_path, ["sh", "-c", "echo out; echo err >&2"], stderr=False)
        text = (tmp_path / "logs" / "a.out").read_text()
        assert "out\n" in text
        assert "err\n" in text
        assert not (tmp_path / "logs" / "a.err").exists()

    def test_appends(self, tmp_path: Path) -> None:
        """A second run adds to the logs instead of replacing them."""
        run_minder(tmp_path, ["sh", "-c", "echo one"])
        run_minder(tmp_path, ["sh", "-c", "echo two"])
        assert (tmp_path / "logs" / "a.out").read_text() == "one\ntwo\n"

    def test_missing_program(self, tmp_path: Path) -> None:
        """A program that cannot start gives a start_error and exit 127."""
        code = run_minder(tmp_path, ["no-such-prog-xyz"])
        assert code == START_ERROR_EXIT
        status = read_json(tmp_path / "s.json")
        assert "No such file" in str(status["start_error"])
        assert status["exit_code"] is None
        assert status["signal"] is None
        assert status["started_at"] == status["ended_at"]

    def test_sigterm_forwarded(self, tmp_path: Path) -> None:
        """SIGTERM to the minder reaches the child, and the minder still writes the status."""
        cmd = [
            sys.executable,
            "-I",
            "-m",
            "pyscheduler.minder",
            "--status",
            str(tmp_path / "s.json"),
        ]
        cmd += ["--stdout", str(tmp_path / "a.out"), "--"]
        ready = tmp_path / "ready"
        cmd += ["sh", "-c", f'trap "exit 7" TERM; touch "{ready}"; sleep 5 & wait']
        proc = subprocess.Popen(cmd)  # noqa: S603
        try:
            wait_for(tmp_path / "s.json.pid")
            wait_for(ready)  # sh has installed its trap
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=RUN_TIMEOUT_SECONDS)
        finally:
            proc.kill()
        assert code == EXIT_SEVEN
        assert read_json(tmp_path / "s.json")["exit_code"] == EXIT_SEVEN

    def test_group_sigterm(self, tmp_path: Path) -> None:
        """SIGTERM to the whole process group still leaves a status behind."""
        cmd = [
            sys.executable,
            "-I",
            "-m",
            "pyscheduler.minder",
            "--status",
            str(tmp_path / "s.json"),
        ]
        ready = tmp_path / "ready"
        cmd += ["--stdout", str(tmp_path / "a.out"), "--"]
        cmd += ["sh", "-c", f'trap "exit 7" TERM; touch "{ready}"; sleep 5 & wait']
        proc = subprocess.Popen(cmd, start_new_session=True)  # noqa: S603
        try:
            wait_for(ready)
            os.killpg(proc.pid, signal.SIGTERM)
            code = proc.wait(timeout=RUN_TIMEOUT_SECONDS)
        finally:
            proc.kill()
        assert code == EXIT_SEVEN
        assert read_json(tmp_path / "s.json")["exit_code"] == EXIT_SEVEN

    def test_shadowing_module_in_cwd(self, tmp_path: Path) -> None:
        """A json.py in the job's cwd cannot break the minder (it runs with -I)."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "json.py").write_text("raise SystemExit('shadowed')\n")
        code = run_minder(tmp_path, ["sh", "-c", "exit 3"], cwd=work)
        assert code == EXIT_THREE
        assert read_json(tmp_path / "s.json")["exit_code"] == EXIT_THREE

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_unwritable_log_dir(self, tmp_path: Path) -> None:
        """A log that cannot be opened gives a start_error note and exit 127."""
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            cmd = [sys.executable, "-I", "-m", "pyscheduler.minder"]
            cmd += ["--status", str(tmp_path / "s.json"), "--stdout", str(locked / "a.out")]
            cmd += ["--", "true"]
            code = subprocess.run(cmd, check=False, timeout=RUN_TIMEOUT_SECONDS).returncode  # noqa: S603
        finally:
            locked.chmod(0o700)
        assert code == START_ERROR_EXIT
        status = read_json(tmp_path / "s.json")
        assert str(status["start_error"]).startswith("cannot open log: ")
        assert status["exit_code"] is None

    def test_signal_death(self, tmp_path: Path) -> None:
        """A child killed by a signal gives signal n, no exit code, and exit 128 + n."""
        code = run_minder(tmp_path, ["sh", "-c", "kill -9 $$"])
        assert code == SIGKILL_EXIT
        status = read_json(tmp_path / "s.json")
        assert status["signal"] == signal.SIGKILL
        assert status["exit_code"] is None


class TestMinderInProcess:
    """The minder's main() and write_status() called directly."""

    def test_main_exit_code(self, tmp_path: Path) -> None:
        """main() returns the child's exit code and restores signal handlers."""
        before = signal.getsignal(signal.SIGTERM)
        status = tmp_path / "deep" / "s.json"
        code = main(
            ["--status", str(status), "--stdout", str(tmp_path / "o"), "--", "sh", "-c", "exit 3"]
        )
        assert code == EXIT_THREE
        assert read_json(status)["exit_code"] == EXIT_THREE
        assert signal.getsignal(signal.SIGTERM) is before

    def test_main_start_error(self, tmp_path: Path) -> None:
        """main() returns 127 when the program cannot start."""
        status = tmp_path / "s.json"
        code = main(["--status", str(status), "--stdout", str(tmp_path / "o"), "--", str(tmp_path)])
        assert code == START_ERROR_EXIT
        assert read_json(status)["start_error"]

    def test_main_log_open_error(self, tmp_path: Path) -> None:
        """main() writes a cannot-open-log note and returns 127."""
        blocker = tmp_path / "file"
        blocker.write_text("")
        status = tmp_path / "s.json"
        argv = ["--status", str(status), "--stdout", str(blocker / "a.out"), "--", "true"]
        assert main(argv) == START_ERROR_EXIT
        assert str(read_json(status)["start_error"]).startswith("cannot open log: ")

    def test_main_keeps_ignoring_signals_without_restore(self, tmp_path: Path) -> None:
        """With restore_signals false, SIGTERM is ignored after the note is written."""
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        argv = ["--status", str(tmp_path / "s.json"), "--stdout", str(tmp_path / "o")]
        try:
            assert main([*argv, "--", "true"], restore_signals=False) == 0
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        finally:
            for sig, handler in before.items():
                signal.signal(sig, handler)

    def test_main_signal_death(self, tmp_path: Path) -> None:
        """main() returns 128 + n for a child killed by signal n."""
        status = tmp_path / "s.json"
        argv = ["--status", str(status), "--stdout", str(tmp_path / "o")]
        argv += ["--stderr", str(tmp_path / "e"), "--", "sh", "-c", "kill -9 $$"]
        assert main(argv) == SIGKILL_EXIT
        assert read_json(status)["signal"] == signal.SIGKILL

    def test_write_status_atomic(self, tmp_path: Path) -> None:
        """write_status replaces the file and leaves no temp file behind."""
        path = tmp_path / "s.json"
        path.write_text("old")
        write_status(path, {"exit_code": 0})
        assert read_json(path) == {"exit_code": 0}
        assert sorted(p.name for p in tmp_path.iterdir()) == ["s.json"]
        assert not list(tmp_path.glob(f"*.tmp-{os.getpid()}"))


class TestForwarder:
    """The signal forwarder used while the child runs."""

    def test_early_signal_is_delivered_on_adopt(self) -> None:
        """A signal that arrives before the child starts is sent once it exists."""
        forwarder = _Forwarder()
        forwarder.handle(signal.SIGTERM, None)
        assert forwarder.pending == [signal.SIGTERM]
        child = subprocess.Popen(["sleep", "5"])  # noqa: S607
        try:
            forwarder.adopt(child.pid)
            assert child.wait(timeout=RUN_TIMEOUT_SECONDS) == -signal.SIGTERM
        finally:
            child.kill()
        assert forwarder.pending == []

    def test_gone_child_is_ignored(self) -> None:
        """A child that has gone (``kill`` fails) is ignored, and nothing is queued."""
        forwarder = _Forwarder()
        forwarder.send(signal.SIGTERM)  # no child yet: nothing happens
        child = subprocess.Popen(["true"])  # noqa: S607
        child.wait(timeout=RUN_TIMEOUT_SECONDS)  # reaped: its pid is gone
        forwarder.adopt(child.pid)
        forwarder.handle(signal.SIGTERM, None)
        assert forwarder.pending == []
