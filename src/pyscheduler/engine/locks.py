"""One key per run: a run is changed by one task at a time.

A run pulling into a station is halfway through a change. ``RunLocks``
hands out one key per run, so an attempt's ending and an operator's
button press never move the same run at once; whoever comes second
waits for the key. A key is made when first wanted and thrown away when
nobody holds or waits for it, so idle runs cost nothing.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from pynet.loop import Lock


@dataclass
class _Key:
    """One run's lock and how many tasks hold or wait for it."""

    lock: Lock = field(default_factory=Lock)
    users: int = 0


class RunLocks:
    """Hand out one lock per run id, made lazily and dropped when idle."""

    def __init__(self) -> None:
        """Start with no locks."""
        self._keys: dict[str, _Key] = {}

    def __len__(self) -> int:
        """Return how many runs have a lock held or waited for."""
        return len(self._keys)

    @asynccontextmanager
    async def hold(self, run_id: str) -> AsyncGenerator[None]:
        """Hold ``run_id``'s lock for the ``async with`` block."""
        key = self._keys.setdefault(run_id, _Key())
        key.users += 1
        try:
            async with key.lock:
                yield
        finally:
            key.users -= 1
            if not key.users:
                del self._keys[run_id]
