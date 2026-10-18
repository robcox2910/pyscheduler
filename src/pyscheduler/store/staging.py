"""Address-book changes made during a commit, kept apart until it succeeds."""

from pydb.record import Value
from pydb.table import Table


class Staged[K]:
    """Hold address-book changes made during a commit, until it succeeds."""

    def __init__(self, base: dict[K, int]) -> None:
        """Start with no changes on top of ``base``."""
        self.base = base
        self.changes: dict[K, int | None] = {}

    def get(self, key: K) -> int | None:
        """Return the record id of ``key``, seeing this commit's changes first."""
        if key in self.changes:
            return self.changes[key]
        return self.base.get(key)

    def put(self, key: K, record_id: int | None) -> None:
        """Note that ``key`` now lives at ``record_id`` (``None``: deleted)."""
        self.changes[key] = record_id

    def publish(self) -> None:
        """Copy the changes into the real address book."""
        for key, record_id in self.changes.items():
            if record_id is None:
                self.base.pop(key, None)
            else:
                self.base[key] = record_id


def upsert[K](table: Table, ids: Staged[K], key: K, row: dict[str, Value]) -> None:
    """Update the row stored under ``key``, or insert it if there is none."""
    record_id = ids.get(key)
    if record_id is None:
        ids.put(key, table.insert(row).record_id)
    else:
        table.update(record_id, row)
