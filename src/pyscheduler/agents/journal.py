"""The agent's own notebook: which attempts it took on, and news not yet heard.

An agent may be stopped, crash or lose its line at any moment, so it
writes down two things in a journalled PyDB database (P4-R10), like a
courier's logbook:

- ``accepted``: every ``(run_id, attempt)`` it ever agreed to run, with
  where its logs and status note go, whether it is still running, and
  how it ended. An order for an attempt already in the book is never run
  again.
- ``pending``: ``finished`` reports the scheduler has not acknowledged.
  They are sent again and again until a ``finished_ack`` crosses them off.

Every method is synchronous and holds the database lock: call them from a
worker thread (``run_in_thread``), never on the event loop.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast

from pydb.database import Database
from pydb.record import Record
from pydb.schema import Column, Schema
from pydb.table import Table
from pydb.transaction import Transaction
from pydb.types import DataType
from pynet.messaging import JsonValue

from pyscheduler.agents.messages import RunKey

__all__ = ["ACCEPTED", "FINISHED", "PENDING", "RUNNING", "Entry", "Journal", "Report"]

ACCEPTED = "accepted"
PENDING = "pending"
RUNNING = "running"
FINISHED = "finished"

T = DataType.TEXT
INT = DataType.INTEGER
TABLES: dict[str, Schema] = {
    ACCEPTED: Schema(
        [
            Column("run_id", T, not_null=True),
            Column("attempt", INT, not_null=True),
            Column("state", T, not_null=True),
            Column("finished", T),
            Column("stdout_path", T, not_null=True),
            Column("stderr_path", T, not_null=True),
            Column("status_path", T, not_null=True),
        ]
    ),
    PENDING: Schema(
        [
            Column("run_id", T, not_null=True),
            Column("attempt", INT, not_null=True),
            Column("report", T, not_null=True),
        ]
    ),
}


type Report = dict[str, JsonValue]
"""An unsigned ``finished`` payload."""


@dataclass(frozen=True)
class Entry:
    """One accepted attempt.

    Attributes:
        run_id: The run.
        attempt: The attempt number.
        state: ``running`` or ``finished``.
        finished: The ``finished`` report once it ended, else ``None``.
        stdout_path: Where its standard output goes on this agent.
        stderr_path: Where its standard error goes on this agent.
        status_path: Where its minder writes the status note.

    """

    run_id: str
    attempt: int
    state: str
    finished: Report | None
    stdout_path: Path
    stderr_path: Path
    status_path: Path

    @property
    def key(self) -> RunKey:
        """Return ``(run_id, attempt)``."""
        return self.run_id, self.attempt


def _key(record: Record) -> RunKey:
    """Return the ``(run_id, attempt)`` of a row."""
    return cast("str", record["run_id"]), cast("int", record["attempt"])


def _report(text: object) -> Report | None:
    """Return the report stored as JSON ``text``, or ``None``."""
    if not isinstance(text, str):
        return None
    return cast("Report", json.loads(text))


def _entry(record: Record) -> Entry:
    """Turn an ``accepted`` row into an ``Entry``."""
    run_id, attempt = _key(record)
    return Entry(
        run_id=run_id,
        attempt=attempt,
        state=cast("str", record["state"]),
        finished=_report(record["finished"]),
        stdout_path=Path(cast("str", record["stdout_path"])),
        stderr_path=Path(cast("str", record["stderr_path"])),
        status_path=Path(cast("str", record["status_path"])),
    )


class Journal:
    """The agent's accepted attempts and unacked reports; use ``open``."""

    def __init__(self, db: Database) -> None:
        """Wrap an open database whose tables exist, and index its rows by attempt."""
        self._db = db
        self._accepted: Table = db.get_table(ACCEPTED)
        self._pending: Table = db.get_table(PENDING)
        self._accepted_ids = {_key(r): r.record_id for r in self._accepted.select()}
        self._pending_ids = {_key(r): r.record_id for r in self._pending.select()}

    @classmethod
    def open(cls, state_dir: Path, *, sync: bool = True) -> Self:
        """Open (or create) the journal in ``state_dir``."""
        state_dir.mkdir(parents=True, exist_ok=True)
        db = Database(state_dir / "journal", journal=True, sync=sync)
        try:
            existing = set(db.table_names())
            for name, schema in TABLES.items():
                if name not in existing:
                    db.create_table(name, schema)
            return cls(db)
        except BaseException:
            db.close()
            raise

    def close(self) -> None:
        """Write a checkpoint, then close the database."""
        with self._db.lock:
            self._db.checkpoint()
            self._db.close()

    def accept(self, entry: Entry) -> tuple[Entry, bool]:
        """Record ``entry`` unless its attempt is already known.

        Returns:
            The stored entry (the old one for a repeat) and whether it is new.

        """
        with self._db.lock:
            known = self._accepted_ids.get(entry.key)
            if known is not None:
                return _entry(self._accepted.get(known)), False
            with Transaction(self._db):
                record = self._accepted.insert(
                    {
                        "run_id": entry.run_id,
                        "attempt": entry.attempt,
                        "state": RUNNING,
                        "finished": None,
                        "stdout_path": str(entry.stdout_path),
                        "stderr_path": str(entry.stderr_path),
                        "status_path": str(entry.status_path),
                    }
                )
            self._accepted_ids[entry.key] = record.record_id
            return _entry(record), True

    def finish(self, key: RunKey, report: Report) -> None:
        """Mark ``key`` finished with ``report`` and add the report to ``pending``, in one commit.

        Raises:
            KeyError: If ``key`` was never accepted.

        """
        text = json.dumps(report, sort_keys=True)
        with self._db.lock:
            accepted_id = self._accepted_ids[key]
            pending_id = self._pending_ids.get(key)
            with Transaction(self._db):
                self._accepted.update(accepted_id, {"state": FINISHED, "finished": text})
                if pending_id is None:
                    row = {"run_id": key[0], "attempt": key[1], "report": text}
                    pending_id = self._pending.insert(row).record_id
                else:
                    self._pending.update(pending_id, {"report": text})
            self._pending_ids[key] = pending_id

    def acked(self, key: RunKey) -> bool:
        """Drop ``key``'s pending report; return whether there was one."""
        with self._db.lock:
            pending_id = self._pending_ids.get(key)
            if pending_id is None:
                return False
            with Transaction(self._db):
                self._pending.delete(pending_id)
            del self._pending_ids[key]
            return True

    def get(self, key: RunKey) -> Entry | None:
        """Return the accepted entry for ``key``, or ``None``."""
        with self._db.lock:
            record_id = self._accepted_ids.get(key)
            return None if record_id is None else _entry(self._accepted.get(record_id))

    def running(self) -> list[Entry]:
        """Return every accepted attempt not yet finished, oldest first."""
        with self._db.lock:
            return [_entry(r) for r in self._accepted.select() if r["state"] == RUNNING]

    def pending(self) -> dict[RunKey, Report]:
        """Return every unacked finished report by attempt."""
        with self._db.lock:
            found: dict[RunKey, Report] = {}
            for record in self._pending.select():
                report = _report(record["report"])
                if report is not None:
                    found[_key(record)] = report
            return found
