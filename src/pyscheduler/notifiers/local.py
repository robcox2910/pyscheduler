"""The notifiers that stay on this machine: write a log line, or run a command.

``LogNotifier`` is a notice-board: every alert is pinned up as one line.
``CommandNotifier`` is a phone call to a helper program: it is handed the
alert on its stdin and does whatever it likes with it.
"""

import contextlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pynet.loop
from pynet.messaging import JsonValue
from pynet.process import Process, create_process

from pyscheduler.config.model import NotifierDef, Settings
from pyscheduler.notifiers.base import NotifyError, check_options
from pyscheduler.values import format_duration

SHELL = "/bin/sh"


class LogNotifier:
    """Append each alert to a file as one JSON line."""

    def __init__(self, path: Path) -> None:
        """Remember the file to append to."""
        self.path = path

    @classmethod
    def from_config(cls, definition: NotifierDef, settings: Settings) -> LogNotifier:
        """Build from ``path`` (relative paths are next to the config file)."""
        del settings
        check_options(definition, required=("path",))
        raw = definition.options["path"]
        if not isinstance(raw, str) or not raw:
            message = "option 'path' for log notifier must be text"
            raise ValueError(message)
        return cls(Path(definition.source.file).parent / raw)

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Append the alert as a line, creating the folders if needed."""
        line = json.dumps(alert, sort_keys=True) + "\n"
        try:
            await pynet.loop.run_in_thread(self._append, line)
        except OSError as exc:
            message = f"cannot write {self.path}: {exc.strerror or exc}"
            raise NotifyError(message) from exc

    def _append(self, line: str) -> None:
        """Write one line (a helper thread)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


class CommandNotifier:
    """Run a command with the alert JSON on its stdin."""

    def __init__(self, argv: Sequence[str], timeout_seconds: float, timeout_text: str) -> None:
        """Remember the command, its time limit, and how to print that limit."""
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds
        self.timeout_text = timeout_text

    @classmethod
    def from_config(cls, definition: NotifierDef, settings: Settings) -> CommandNotifier:
        """Build from ``command``: text for ``/bin/sh -c``, or a list of arguments."""
        del settings
        check_options(definition, required=("command",))
        raw = definition.options["command"]
        argv: list[str]
        if isinstance(raw, str) and raw:
            argv = [SHELL, "-c", raw]
        elif (
            isinstance(raw, list)
            and raw
            and all(isinstance(a, str) for a in cast("list[object]", raw))
        ):
            argv = list(cast("list[str]", raw))
        else:
            message = "option 'command' for command notifier must be text or a list of text"
            raise ValueError(message)
        return cls(argv, definition.timeout.total_seconds(), format_duration(definition.timeout))

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Run the command, feed it the alert, and wait for it within the timeout."""
        try:
            process = await create_process(self.argv, stdin=True, stdout=True, stderr=True)
        except OSError as exc:
            message = f"cannot start command: {exc.strerror or exc}"
            raise NotifyError(message) from exc
        payload = json.dumps(alert, sort_keys=True).encode()
        try:
            code, err = await pynet.loop.wait_for(
                self._talk(process, payload), self.timeout_seconds
            )
        except TimeoutError:
            process.kill_group()
            message = f"command timed out after {self.timeout_text}"
            raise NotifyError(message) from None
        except BaseException:
            process.kill_group()  # cancelled or broken: never leave the command running
            raise
        finally:
            process.close()
        if code != 0:
            last = next((ln for ln in reversed(err.splitlines()) if ln.strip()), "")
            message = f"command exited {code}: {last}" if last else f"command exited {code}"
            raise NotifyError(message)

    @staticmethod
    async def _talk(process: Process, payload: bytes) -> tuple[int, str]:
        """Feed stdin while draining stdout and stderr; return (exit code, stderr text)."""
        assert process.stdout is not None  # noqa: S101 -- asked for in create_process
        assert process.stderr is not None  # noqa: S101
        stdout, stderr = process.stdout, process.stderr

        async def feed() -> None:
            """Write the alert and close stdin (the command may not read it)."""
            assert process.stdin is not None  # noqa: S101
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(payload)
                await process.stdin.drain()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                process.stdin.close()

        results = await pynet.loop.gather(feed(), stdout.read(), stderr.read(), process.wait())
        raw_err = results[2]
        code = results[3]
        assert isinstance(raw_err, bytes)  # noqa: S101
        assert isinstance(code, int)  # noqa: S101
        return code, raw_err.decode("utf-8", errors="replace")


__all__ = ["CommandNotifier", "LogNotifier"]
