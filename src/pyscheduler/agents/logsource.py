"""Log sources: read an attempt's log from a file here, or from an agent over there.

Reading a log is like reading a book, and a book can be on your own shelf
or on a shelf at another branch. A ``LogSource`` is the librarian who
fetches pages for you without you caring which. ``LocalLogSource`` walks
to the shelf in the next room (a file on this machine);
``RemoteLogSource`` phones the other branch (the agent that ran the
attempt, through the hub). Both answer the same three questions:

- ``tail`` -- the last lines (the API's log route);
- ``first_look`` -- the last whole lines and where to carry on reading
  from (the live tail's first page);
- ``read`` -- the bytes from an offset on (each poll of the live tail).

If the other branch is shut or does not pick up, the remote librarian
says so with ``LogUnavailableError`` ("the agent is offline", "the agent did
not answer").
"""

import os
from pathlib import Path
from typing import Protocol

from pynet.loop import run_in_thread

from pyscheduler.agents.hub_link import AgentOffline
from pyscheduler.agents.log_messages import MAX_READ_BYTES, MAX_TAIL_LINES, LogReply
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt
from pyscheduler.engine.remote import Placement, remote_agent

__all__ = [
    "LOG_CHUNK",
    "NO_ANSWER",
    "OFFLINE",
    "LocalLogSource",
    "LogSource",
    "LogUnavailableError",
    "RemoteLogSource",
    "log_source_for",
    "read_from",
    "read_tail",
]

MAX_TAIL_BYTES = 8 * 1024 * 1024
"""The most bytes read from the end of a log, however long its lines."""

BLOCK = 64 * 1024
"""How many bytes the tail reader steps back at a time."""

LOG_CHUNK = 65536
"""The most bytes read from the log file per look (and the longest held-back line)."""

OFFLINE = "the agent is offline"
"""Why a remote log cannot be read while its agent is away."""

NO_ANSWER = "the agent did not answer"
"""Why a remote log cannot be read when its agent stays silent."""


class LogUnavailableError(Exception):
    """Raised when a log cannot be read right now; the message says why (safe to show)."""


def read_tail(path: Path, count: int, *, end: int | None = None) -> tuple[list[str], bool] | None:
    """Return the last ``count`` lines of ``path`` and whether earlier text was left out.

    The file is read backwards in ``BLOCK``-sized steps, never more than
    ``MAX_TAIL_BYTES``, so a huge log costs no more than its tail. Bytes
    that are not UTF-8 become U+FFFD. With ``end`` only the first ``end``
    bytes count (what a live tail has seen so far). Returns ``None`` if
    the file does not exist.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    with handle:
        pos = handle.seek(0, os.SEEK_END)
        if end is not None:
            pos = min(pos, end)
        data = b""
        while pos > 0 and len(data) < MAX_TAIL_BYTES:
            body = data[:-1] if data.endswith(b"\n") else data
            if body.count(b"\n") >= count:
                break
            step = min(BLOCK, pos, MAX_TAIL_BYTES - len(data))
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    if data.endswith(b"\n"):
        data = data[:-1]
    parts = data.split(b"\n") if data else []
    truncated = pos > 0
    if truncated and parts:
        parts = parts[1:]  # it may start part-way through a line
    if len(parts) > count:
        parts = parts[-count:]
        truncated = True
    lines = [part.decode("utf-8", errors="replace").removesuffix("\r") for part in parts]
    return lines, truncated


def read_from(path: Path, offset: int, limit: int) -> tuple[bytes, int, bool]:
    """Return up to ``limit`` bytes of ``path`` from ``offset``, the new offset, and if it shrank.

    A file shorter than ``offset`` was rotated: it is read from 0 instead
    and the flag is true. A missing file reads as nothing new.

    Raises:
        OSError: If the file is there but cannot be read (say, no permission).

    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return b"", offset, False
    with handle:
        size = handle.seek(0, os.SEEK_END)
        shrank = size < offset
        start = 0 if shrank else offset
        handle.seek(start)
        data = handle.read(limit)
    return data, start + len(data), shrank


def _first_look(path: Path, count: int, chunk: int) -> tuple[list[str], int]:
    """Return the last ``count`` whole lines of ``path`` and the offset just after them.

    An unfinished last line is left for the polls, so it is sent whole.
    (A last line longer than ``chunk`` is sent as it stands.)

    Raises:
        OSError: If the file is there but cannot be read.

    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], 0
    back = min(size, chunk)
    data, _, _ = read_from(path, size - back, back)
    cut = data.rfind(b"\n")
    if cut >= 0:
        end = size - back + cut + 1
    elif back == size:
        end = 0  # the whole file is one unfinished line
    else:
        end = size
    found = read_tail(path, count, end=end) if end else None
    return (found[0] if found else []), end


class LogSource(Protocol):
    """Where one attempt stream's log is read from (a file here, or an agent's)."""

    async def tail(self, count: int) -> tuple[list[str], bool] | None:
        """Return the last ``count`` lines and whether earlier text was left out.

        Returns ``None`` if there is no such log (yet).

        Raises:
            LogUnavailableError: If the log cannot be reached now.
            OSError: If a local file is there but cannot be read.

        """
        ...

    async def first_look(self, count: int) -> tuple[list[str], int]:
        """Return the last ``count`` whole lines and the offset just after them.

        Raises:
            LogUnavailableError: If the log cannot be reached now.
            OSError: If a local file is there but cannot be read.

        """
        ...

    async def read(self, offset: int, limit: int) -> tuple[bytes, int, bool]:
        """Return up to ``limit`` bytes from ``offset``, the new offset, and if the log shrank.

        Raises:
            LogUnavailableError: If the log cannot be reached now.
            OSError: If a local file is there but cannot be read.

        """
        ...


class LocalLogSource:
    """Read a log file on this machine (on a helper thread).

    Args:
        path: The log file.
        chunk: The most bytes ``first_look`` looks back over.

    """

    def __init__(self, path: Path, chunk: int = LOG_CHUNK) -> None:
        """Remember the file."""
        self.path = path
        self.chunk = chunk

    async def tail(self, count: int) -> tuple[list[str], bool] | None:
        """Return the file's last ``count`` lines, or ``None`` if it is not there."""
        return await run_in_thread(read_tail, self.path, count)

    async def first_look(self, count: int) -> tuple[list[str], int]:
        """Return the last ``count`` whole lines and where to carry on."""
        return await run_in_thread(_first_look, self.path, count, self.chunk)

    async def read(self, offset: int, limit: int) -> tuple[bytes, int, bool]:
        """Return what was added since ``offset`` (a rotated file restarts at 0)."""
        return await run_in_thread(read_from, self.path, offset, limit)


class RemoteLogSource:
    """Read an attempt's log from the agent that ran it, through the hub.

    A log the agent does not have reads as nothing yet, like a local file
    that is not there.

    Args:
        hub: Where to ask (``None``: this scheduler has no hub, so the agent is out of reach).
        agent: The agent that ran the attempt.
        run_id: The run.
        attempt: The attempt number.
        stream: ``stdout`` or ``stderr``.

    """

    def __init__(
        self, hub: Placement | None, agent: str, run_id: str, attempt: int, stream: str
    ) -> None:
        """Remember whom to ask for what."""
        self.hub, self.agent = hub, agent
        self.run_id, self.attempt, self.stream = run_id, attempt, stream

    async def tail(self, count: int) -> tuple[list[str], bool] | None:
        """Return the agent's last ``count`` lines, or ``None`` if it has no such log."""
        reply = await self._ask(mode="tail", count=min(count, MAX_TAIL_LINES))
        if reply.missing:
            return None
        return reply.lines or [], reply.truncated

    async def first_look(self, count: int) -> tuple[list[str], int]:
        """Return the agent's last ``count`` lines and the log size they were read at."""
        reply = await self._ask(mode="tail", count=min(count, MAX_TAIL_LINES))
        if reply.missing:
            return [], 0
        return reply.lines or [], reply.offset

    async def read(self, offset: int, limit: int) -> tuple[bytes, int, bool]:
        """Return what the agent's log gained since ``offset``."""
        reply = await self._ask(mode="read", offset=offset, limit=min(limit, MAX_READ_BYTES))
        if reply.missing:
            return b"", offset, False
        return reply.data or b"", reply.offset, reply.shrank

    async def _ask(
        self,
        *,
        mode: str,
        count: int | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> LogReply:
        """Fetch one reply from the hub, turning its failures into ``LogUnavailableError``."""
        if self.hub is None:
            raise LogUnavailableError(OFFLINE)
        try:
            return await self.hub.fetch_log(
                self.agent,
                self.run_id,
                self.attempt,
                self.stream,
                mode=mode,
                count=count,
                offset=offset,
                limit=limit,
            )
        except AgentOffline:
            raise LogUnavailableError(OFFLINE) from None
        except TimeoutError:
            raise LogUnavailableError(NO_ANSWER) from None


def log_source_for(
    engine: Engine, row: Attempt, stream: str, *, chunk: int = LOG_CHUNK
) -> LogSource:
    """Return where ``row``'s ``stream`` log is read from: the agent that ran it, or a local file."""
    agent = remote_agent(row)
    if agent is None:
        path = row.stdout_path if stream == "stdout" else row.stderr_path
        return LocalLogSource(Path(path), chunk)
    return RemoteLogSource(engine.placement, agent, row.run_id, row.attempt, stream)
