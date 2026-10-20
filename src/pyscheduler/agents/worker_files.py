"""The agent's file work: where an attempt's files go, what a restart finds, reading logs.

Everything here touches the disk (or runs ``ps``), so it is blocking:
the agent calls it through ``run_in_thread``, never on the event loop.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pyscheduler.agents.journal import Entry
from pyscheduler.agents.log_messages import LogReply, LogRequest
from pyscheduler.agents.logsource import read_from, read_tail
from pyscheduler.agents.messages import WorkOrder
from pyscheduler.engine.adopted import minder_alive, read_pid
from pyscheduler.engine.status_note import AttemptResult, read_status

__all__ = ["Found", "attempt_files", "look", "read_log"]

logger = logging.getLogger("pyscheduler.agents.worker")


def attempt_files(order: WorkOrder, log_dir: Path, state_dir: Path) -> tuple[Path, Path, Path]:
    """Return the stdout, stderr and status-note paths of ``order`` on this agent (P4-R6).

    Logs go to ``{log_dir}/{job}/{run_date}/{run_id}.{attempt}.out`` and
    ``.err``; the minder's note goes to ``{state_dir}/status/{run_id}.{attempt}.json``.

    Raises:
        ValueError: If a path would leave its folder (a second guard behind
            the payload checks, which already refuse ``/`` and ``..``).

    """
    stem = f"{order.run_id}.{order.attempt}"
    folder = log_dir / order.job / order.run_date
    paths = (folder / f"{stem}.out", folder / f"{stem}.err", state_dir / "status" / f"{stem}.json")
    for path, root in zip(paths, (log_dir, log_dir, state_dir), strict=True):
        if not path.resolve().is_relative_to(root.resolve()):
            msg = "an attempt's files would leave the agent's folders"
            raise ValueError(msg)
    return paths


@dataclass(frozen=True)
class Found:
    """What a restarted agent finds for an attempt its journal says is running.

    Attributes:
        result: How it ended, if the minder left its note.
        pid: The minder's pid, if it is still alive (and still the minder).
        started_at: When that minder started.

    """

    result: AttemptResult | None = None
    pid: int | None = None
    started_at: datetime | None = None


def look(status_path: Path) -> Found:
    """Look for the note, else a live minder, of the attempt whose note goes to ``status_path``.

    The note is read again after a minder is found gone, because the
    minder writes its note just before it exits.
    """
    result = read_status(status_path)
    if result is not None:
        return Found(result)
    note = read_pid(status_path)
    began = note.started_at if note is not None else None
    if note is not None and began is not None and minder_alive(note.pid, began):
        return Found(pid=note.pid, started_at=began)
    return Found(read_status(status_path))


def read_log(entry: Entry | None, request: LogRequest) -> LogReply:
    """Answer ``request`` from the log files of ``entry`` (``None``: never accepted here).

    A missing or unreadable file is answered as ``missing``; only an
    unreadable one is logged (a missing one is ordinary, and asked often).
    """
    missing = LogReply(request.request_id, missing=True)
    if entry is None:
        return missing
    path = entry.stdout_path if request.stream == "stdout" else entry.stderr_path
    try:
        if request.mode == "tail":
            size = path.stat().st_size
            found = read_tail(path, request.count, end=size)
            if found is None:
                return missing
            return LogReply(request.request_id, lines=found[0], truncated=found[1], offset=size)
        if not path.exists():
            return missing
        data, offset, shrank = read_from(path, request.offset, request.limit)
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):  # a missing log is not worth a warning
            logger.warning("cannot read the %s log of %s: %s", request.stream, request.run_id, exc)
        return missing
    return LogReply(request.request_id, data=data, offset=offset, shrank=shrank)
