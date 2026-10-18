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

A minder starts for every attempt, so it travels light. Two hundred
attempts starting at once means two hundred interpreters starting at
once, and every millisecond of start-up is paid two hundred times over.
So the minder uses only ``os``, ``signal``, ``sys`` and ``datetime``: it
reads its own command line (no ``argparse``), starts the child with
``os.posix_spawnp`` (no ``subprocess``), uses plain ``os`` paths (no
``pathlib``) and writes its small JSON notes itself (no ``json``, which
would pull in ``re``). That roughly halves its start-up cost.
"""

import os
import signal
import sys
from datetime import UTC, datetime
from types import FrameType

START_ERROR_EXIT = 127
SIGNAL_EXIT_BASE = 128
USAGE_EXIT = 2
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT)
RESET_SIGNALS = (signal.SIGPIPE, signal.SIGXFSZ)
"""Signals Python ignores that the child gets back at their defaults (as ``subprocess`` does)."""

USAGE = (
    "usage: python -I -m pyscheduler.minder --status PATH --stdout PATH [--stderr PATH] -- ARGV..."
)
LOG_MODE = 0o666
"""The mode new log files are created with (before the umask)."""

_OPTIONS = ("--status", "--stdout", "--stderr")
_ASCII_TOP = 0x7E
_SPACE = 0x20
_BMP_TOP = 0xFFFF
_PLANE = 0x10000
_HIGH_SURROGATE = 0xD800
_LOW_SURROGATE = 0xDC00
_TEN_BITS = 0x3FF
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

type PathLike = str | os.PathLike[str]
type Note = dict[str, str | int | bool | None]


def utc_now() -> str:
    """Return the current instant as ISO 8601 UTC ending in ``Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_string(text: str) -> str:
    """Return ``text`` as a JSON string, escaped exactly as ``json.dumps`` does."""
    parts = ['"']
    for char in text:
        code = ord(char)
        if char in _ESCAPES:
            parts.append(_ESCAPES[char])
        elif _SPACE <= code <= _ASCII_TOP:
            parts.append(char)
        elif code <= _BMP_TOP:
            parts.append(f"\\u{code:04x}")
        else:  # outside the basic plane: a surrogate pair
            code -= _PLANE
            high = _HIGH_SURROGATE | (code >> 10)
            low = _LOW_SURROGATE | (code & _TEN_BITS)
            parts.append(f"\\u{high:04x}\\u{low:04x}")
    parts.append('"')
    return "".join(parts)


def _json_value(value: object) -> str:
    """Return one note value (a string, int, bool or None) as JSON.

    Raises:
        TypeError: For any other kind of value.

    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _json_string(value)
    message = f"a status note holds strings, ints, bools and None, not {value!r}"
    raise TypeError(message)


def dumps(note: Note) -> str:
    """Return ``note`` as a JSON object (the same text ``json.dumps`` gives)."""
    items = (f"{_json_string(key)}: {_json_value(value)}" for key, value in note.items())
    return "{" + ", ".join(items) + "}"


def write_status(path: PathLike, status: Note) -> None:
    """Write ``status`` as JSON to ``path`` atomically.

    The JSON goes to a temp file in the same directory, is flushed and
    fsynced, then renamed over ``path``; the directory is fsynced too. A
    reader therefore sees either the old file or the whole new one, never
    half a note.

    Raises:
        OSError: If the note cannot be written.
        TypeError: If a value is not a string, int, bool or None.

    """
    target = os.fspath(path)
    folder = os.path.dirname(target) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = f"{target}.tmp-{os.getpid()}"
    data = dumps(status).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, LOG_MODE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _Forwarder:
    """Pass signals on to the child, remembering any that arrive before it starts."""

    def __init__(self) -> None:
        """Start with no child and no pending signals."""
        self.child: int | None = None
        self.pending: list[int] = []

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        """Forward ``signum`` to the child, or keep it until the child exists."""
        if self.child is None:
            self.pending.append(signum)
        else:
            self.send(signum)

    def adopt(self, pid: int) -> None:
        """Take charge of the child ``pid`` and deliver any signals that came early."""
        self.child = pid
        for signum in self.pending:
            self.send(signum)
        self.pending.clear()

    def send(self, signum: int) -> None:
        """Send ``signum`` to the child, ignoring a child that has already gone.

        The minder forgets the child (``child = None``) as soon as it has
        reaped it, so a late signal is never sent to a pid that now belongs
        to someone else.
        """
        if self.child is not None:
            try:
                os.kill(self.child, signum)
            except OSError:
                pass


class MinderArgs:
    """The minder's command line, read.

    Attributes:
        status: Where to write the status note.
        stdout: The log file for the child's stdout.
        stderr: The log file for its stderr, or None to share stdout's.
        command: The child's command and arguments.

    """

    def __init__(self, status: str, stdout: str, stderr: str | None, command: list[str]) -> None:
        """Hold the parsed options."""
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.command = command


def _usage(problem: str) -> None:
    """Print the usage and ``problem`` to stderr, then exit 2.

    Raises:
        SystemExit: Always, with code 2.

    """
    sys.stderr.write(f"{USAGE}\nminder: error: {problem}\n")
    raise SystemExit(USAGE_EXIT)


def parse_args(argv: list[str] | None = None) -> MinderArgs:
    """Read ``--status PATH --stdout PATH [--stderr PATH] -- ARGV...`` (``argv`` or ``sys.argv``).

    The options come in any order, each once, and every word after ``--``
    belongs to the command (which must not be empty).

    Raises:
        SystemExit: With code 2 for a bad command line (after printing the usage).

    """
    words = list(sys.argv[1:] if argv is None else argv)
    options: dict[str, str] = {}
    while words and words[0] != "--":
        flag = words.pop(0)
        if flag not in _OPTIONS:
            _usage(f"unknown option {flag!r}")
        if flag in options:
            _usage(f"{flag} given twice")
        if not words:
            _usage(f"{flag} needs a value")
        options[flag] = words.pop(0)
    if not words:
        _usage("the command must follow --")
    command = words[1:]
    for required in ("--status", "--stdout"):
        if required not in options:
            _usage(f"{required} is required")
    if not command:
        _usage("no command given after --")
    return MinderArgs(options["--status"], options["--stdout"], options.get("--stderr"), command)


def _open_log(path: str) -> int:
    """Open a log file for appending (creating its folder) and return its descriptor."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LOG_MODE)


def _start_error(status_path: str, message: str) -> int:
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


def _spawn(command: list[str], out: int, err: int | None) -> int:
    """Start ``command`` (searched on ``PATH``) with stdin from /dev/null; return its pid.

    Raises:
        OSError: If it cannot be started.

    """
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
        (os.POSIX_SPAWN_DUP2, out, 1),
        (os.POSIX_SPAWN_DUP2, out if err is None else err, 2),
    ]
    return os.posix_spawnp(
        command[0], command, os.environ, file_actions=actions, setsigdef=RESET_SIGNALS
    )


def _wait(pid: int) -> int:
    """Wait for ``pid`` and return its exit code, or minus the signal that killed it."""
    _, status = os.waitpid(pid, 0)  # retried by Python if a signal interrupts it
    return os.waitstatus_to_exitcode(status)


def _run(
    command: list[str], status_path: str, out: int, err: int | None, *, restore_signals: bool
) -> int:
    """Start the child, wait for it with signals forwarded, and write the status.

    With ``restore_signals`` false (the CLI), SIGTERM/SIGINT are ignored once
    the note is written, so a late group signal cannot change the exit code.
    """
    forwarder = _Forwarder()
    previous = {sig: signal.signal(sig, forwarder.handle) for sig in FORWARDED_SIGNALS}
    try:
        started_at = utc_now()
        write_status(f"{status_path}.pid", {"pid": os.getpid(), "started_at": started_at})
        try:
            pid = _spawn(command, out, err)
        except OSError as exc:
            return _start_error(status_path, str(exc))
        forwarder.adopt(pid)
        returncode = _wait(pid)
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


def main(argv: list[str] | None = None, *, restore_signals: bool = True) -> int:
    """Run the minder and return its exit code.

    The code is the child's exit code, ``128 + n`` if signal ``n`` killed
    the child, or ``127`` if the child could not be started or the logs
    could not be opened. ``restore_signals`` puts the caller's SIGTERM and
    SIGINT handlers back afterwards; the CLI passes false.
    """
    args = parse_args(argv)
    folder = os.path.dirname(args.status)
    if folder:
        try:  # best effort; write_status tries again
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
    opened: list[int] = []
    try:
        try:
            opened.append(_open_log(args.stdout))
            if args.stderr is not None:
                opened.append(_open_log(args.stderr))
        except OSError as exc:
            return _start_error(args.status, f"cannot open log: {exc}")
        err = opened[1] if len(opened) > 1 else None
        return _run(args.command, args.status, opened[0], err, restore_signals=restore_signals)
    finally:
        for fd in opened:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main(restore_signals=False))
