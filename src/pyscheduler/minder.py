"""Run one job attempt and write down how it ended.

Every attempt runs as::

    python -I -m pyscheduler.minder --status PATH --stdout PATH [--stderr PATH] -- ARGV...

``-I`` (isolated mode) matters: the job's working directory is not put on
``sys.path``, so a ``json.py`` lying there cannot shadow the minder's own
imports, and ``PYTHON*`` variables are ignored. The child still gets the
full environment.

The minder opens the log files (appending), writes ``<status>.pid`` with its
own pid, runs ARGV as its child, forwards SIGTERM/SIGINT to the child and
keeps waiting, then writes the status JSON atomically. Because the note is
on disk, the scheduler can learn how an attempt ended even if the scheduler
itself was down at the time. If the logs cannot be opened, the note says
so in ``start_error`` and the minder exits 127.
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import IO, Protocol

START_ERROR_EXIT = 127
SIGNAL_EXIT_BASE = 128
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def utc_now() -> str:
    """Return the current instant as ISO 8601 UTC ending in ``Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_status(path: Path, status: Mapping[str, object]) -> None:
    """Write ``status`` as JSON to ``path`` atomically.

    The JSON goes to a temp file in the same directory, is flushed and
    fsynced, then renamed over ``path``; the directory is fsynced too. A
    reader therefore sees either the old file or the whole new one, never
    half a note.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(dict(status), f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _Signallable(Protocol):
    """Anything that can be sent a signal, such as a ``subprocess.Popen``."""

    def send_signal(self, sig: int) -> None:
        """Send ``sig`` to the process."""
        ...


class _Forwarder:
    """Pass signals on to the child, remembering any that arrive before it starts."""

    def __init__(self) -> None:
        """Start with no child and no pending signals."""
        self.child: _Signallable | None = None
        self.pending: list[int] = []

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        """Forward ``signum`` to the child, or keep it until the child exists."""
        if self.child is None:
            self.pending.append(signum)
        else:
            self.send(signum)

    def adopt(self, child: _Signallable) -> None:
        """Take charge of ``child`` and deliver any signals that came early."""
        self.child = child
        for signum in self.pending:
            self.send(signum)
        self.pending.clear()

    def send(self, signum: int) -> None:
        """Send ``signum`` to the child, ignoring a child that has already gone.

        ``Popen.send_signal`` checks whether the child has been reaped, so a
        signal is never sent to a pid that now belongs to someone else.
        """
        if self.child is not None:
            with contextlib.suppress(OSError):
                self.child.send_signal(signum)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the minder's command line."""
    parser = argparse.ArgumentParser(prog="python -m pyscheduler.minder")
    parser.add_argument("--status", required=True, type=Path)
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--stderr", type=Path)
    parser.add_argument("command", nargs="+")
    return parser.parse_args(argv)


def _open_log(path: Path) -> IO[bytes]:
    """Open a log file for appending, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("ab")


def _start_error(status_path: Path, message: str) -> int:
    """Write a note saying the child never started, and return 127."""
    now = utc_now()
    write_status(
        status_path,
        {
            "exit_code": None,
            "signal": None,
            "started_at": now,
            "ended_at": now,
            "start_error": message,
        },
    )
    return START_ERROR_EXIT


def _run(
    command: list[str],
    status_path: Path,
    out: IO[bytes],
    err: IO[bytes] | None,
    *,
    restore_signals: bool,
) -> int:
    """Start the child, wait for it with signals forwarded, and write the status.

    With ``restore_signals`` false (the CLI), SIGTERM/SIGINT are ignored once
    the note is written, so a late group signal cannot change the exit code.
    """
    forwarder = _Forwarder()
    previous = {sig: signal.signal(sig, forwarder.handle) for sig in FORWARDED_SIGNALS}
    try:
        started_at = utc_now()
        write_status(
            status_path.with_name(f"{status_path.name}.pid"),
            {"pid": os.getpid(), "started_at": started_at},
        )
        try:
            child = subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT if err is None else err,
            )
        except OSError as exc:
            return _start_error(status_path, str(exc))
        forwarder.adopt(child)
        returncode = child.wait()
        # The child is reaped: late signals are now just noted, never sent to a
        # pid that may belong to someone else, and they cannot kill us before
        # the note is written.
        forwarder.child = None
        exit_code, signum = (None, -returncode) if returncode < 0 else (returncode, None)
        write_status(
            status_path,
            {
                "exit_code": exit_code,
                "signal": signum,
                "started_at": started_at,
                "ended_at": utc_now(),
                "start_error": None,
            },
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler if restore_signals else signal.SIG_IGN)
    return SIGNAL_EXIT_BASE + signum if signum is not None else returncode


def main(argv: Sequence[str] | None = None, *, restore_signals: bool = True) -> int:
    """Run the minder and return its exit code.

    The code is the child's exit code, ``128 + n`` if signal ``n`` killed
    the child, or ``127`` if the child could not be started or the logs
    could not be opened. ``restore_signals`` puts the caller's SIGTERM and
    SIGINT handlers back afterwards; the CLI passes false.
    """
    args = _parse(argv)
    status_path: Path = args.status
    with contextlib.suppress(OSError):  # best effort; write_status tries again
        status_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path: Path | None = args.stderr
    with contextlib.ExitStack() as stack:
        try:
            out = stack.enter_context(_open_log(args.stdout))
            err = None if stderr_path is None else stack.enter_context(_open_log(stderr_path))
        except OSError as exc:
            return _start_error(status_path, f"cannot open log: {exc}")
        return _run(list(args.command), status_path, out, err, restore_signals=restore_signals)


if __name__ == "__main__":
    sys.exit(main(restore_signals=False))
