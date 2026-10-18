"""Tests for the log and command notifiers."""

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

import pynet.loop
import pytest
from pynet.messaging import JsonValue

from pyscheduler.config import load_config
from pyscheduler.config.model import NotifierDef
from pyscheduler.notifiers import NotifyError, build_notifier
from pyscheduler.notifiers.local import CommandNotifier, LogNotifier

ALERT: Mapping[str, JsonValue] = {"alert_id": "load.20261017.1:failed:1", "message": "load failed"}
OTHER: Mapping[str, JsonValue] = {"alert_id": "load.20261017.2:failed:2", "message": "again"}
EXIT_CODE = 3
TIMEOUT_LIMIT_SECONDS = 3.0


def load_notifier(tmp_path: Path, body: str) -> NotifierDef:
    """Load a config with one notifier ``n`` (``body`` is its options) and return it."""
    text = f"notifiers:\n  n: {body}\njobs:\n  a: {{command: 'true', schedule: manual}}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path, env={}).notifiers["n"]


def build(tmp_path: Path, body: str) -> CommandNotifier | LogNotifier:
    """Build the notifier ``n`` of a one-notifier config."""
    definition = load_notifier(tmp_path, body)
    config = load_config(tmp_path / "scheduler.yaml", env={})
    notifier = build_notifier(definition, config.settings)
    assert isinstance(notifier, CommandNotifier | LogNotifier)
    return notifier


class TestLogNotifier:
    """The log notifier appends JSON lines."""

    def test_log_notifier_appends_json_lines(self, tmp_path: Path) -> None:
        """Two sends write two lines, next to the config file when the path is relative."""
        notifier = build(tmp_path, "{type: log, path: logs/alerts.log}")

        async def main() -> None:
            """Send two alerts."""
            await notifier.send(ALERT)
            await notifier.send(OTHER)

        pynet.loop.run(main())
        lines = (tmp_path / "logs" / "alerts.log").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in lines] == [ALERT, OTHER]

    def test_log_notifier_unwritable_path_is_a_notify_error(self, tmp_path: Path) -> None:
        """A path under a file cannot be written: the send fails cleanly."""
        (tmp_path / "file").write_text("x", encoding="utf-8")
        notifier = build(tmp_path, "{type: log, path: file/alerts.log}")
        with pytest.raises(NotifyError, match="cannot write"):
            pynet.loop.run(notifier.send(ALERT))


class TestCommandNotifier:
    """The command notifier feeds the alert to a process."""

    def test_command_notifier_gets_alert_on_stdin(self, tmp_path: Path) -> None:
        """The command's stdin holds the alert JSON."""
        notifier = build(tmp_path, f"{{type: command, command: 'cat > {tmp_path}/out.json'}}")
        pynet.loop.run(notifier.send(ALERT))
        assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == ALERT

    def test_command_notifier_list_form(self, tmp_path: Path) -> None:
        """A list command runs without a shell."""
        notifier = build(tmp_path, f"{{type: command, command: [tee, {tmp_path}/out.json]}}")
        pynet.loop.run(notifier.send(ALERT))
        assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == ALERT

    def test_command_notifier_nonzero_exit(self, tmp_path: Path) -> None:
        """A failing command reports its exit code and last stderr line."""
        notifier = build(tmp_path, "{type: command, command: 'echo boom >&2; exit 3'}")
        with pytest.raises(NotifyError) as caught:
            pynet.loop.run(notifier.send(ALERT))
        assert f"exited {EXIT_CODE}" in str(caught.value)
        assert "boom" in str(caught.value)

    def test_command_notifier_missing_program(self, tmp_path: Path) -> None:
        """A program that does not exist is a failed send."""
        notifier = build(tmp_path, f"{{type: command, command: [{tmp_path}/nope]}}")
        with pytest.raises(NotifyError, match="cannot start command"):
            pynet.loop.run(notifier.send(ALERT))

    def test_command_notifier_timeout_kills_group(self, tmp_path: Path) -> None:
        """A slow command is killed with its children when the timeout passes."""
        pid_file = tmp_path / "pgid"
        command = f"echo $$$$ > {pid_file}; sleep 30 & sleep 30"
        notifier = build(tmp_path, f"{{type: command, timeout: 1s, command: '{command}'}}")
        started = time.monotonic()
        with pytest.raises(NotifyError) as caught:
            pynet.loop.run(notifier.send(ALERT))
        assert str(caught.value) == "command timed out after 1s"
        assert time.monotonic() - started < TIMEOUT_LIMIT_SECONDS
        pgid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError, PermissionError:  # macOS: EPERM for a zombie-only group
                return
            time.sleep(0.02)
        pytest.fail("a child of the command is still alive")

    def test_command_notifier_cancel_kills_group(self, tmp_path: Path) -> None:
        """Cancelling a send kills the command and everything it started."""
        pid_file = tmp_path / "pgid"
        command = f"echo $$$$ > {pid_file}; sleep 30 & sleep 30"
        notifier = build(tmp_path, f"{{type: command, command: '{command}'}}")

        async def main() -> None:
            """Start a send, wait for the command to run, then cancel it."""
            task = pynet.loop.spawn(notifier.send(ALERT), name="send")
            deadline = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < deadline:
                await pynet.loop.sleep(0.02)
            task.cancel()
            with pytest.raises(pynet.loop.Cancelled):
                await task

        pynet.loop.run(main())
        pgid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError, PermissionError:  # macOS: EPERM for a zombie-only group
                return
            time.sleep(0.02)
        pytest.fail("a child of the command is still alive")
