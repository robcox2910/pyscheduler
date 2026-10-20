"""Decide which queued runs may start: the global cap and the pools.

Think of a trampoline that only holds two kids at once. Everyone else
waits in line, and each time a kid climbs off, the next ones in line
that fit get on. A **pool** is a smaller trampoline for one special
thing (say, the database): a run that needs ``db`` waits for a ``db``
slot, but runs that do not need it can jump on anyway.

A file watcher is not a kid on the trampoline at all: it is someone
standing by the letterbox, so it takes no global slot (``global_slot``
false) and never keeps a jumper waiting.

``Admission`` counts the slots. ``ReadyQueue`` holds the waiting runs
in priority order and hands out the ones that fit. A run that fits may
still need a seat somewhere else (a remote agent): the optional ``place``
check books it last, so a run refused there holds no slot.
"""

import heapq
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pyscheduler.config.model import Criticality, JobDef, JobKind
from pyscheduler.engine.model import Run


def takes_global_slot(job: JobDef | None) -> bool:
    """Return whether a run of ``job`` holds a global slot (every job but a file watcher)."""
    return job is None or job.kind is not JobKind.FILE_WATCHER


def _check_needs(needs: Mapping[str, int]) -> None:
    """Raise ValueError if any amount in ``needs`` is below one."""
    for pool, amount in needs.items():
        if amount < 1:
            msg = f"need for pool {pool!r} must be at least 1, got {amount}"
            raise ValueError(msg)


class Admission:
    """Count running runs against a global cap and named pool limits."""

    def __init__(self, max_running: int, pools: Mapping[str, int]) -> None:
        """Start with nothing running and the given limits."""
        self._max_running = max_running
        self._pools: dict[str, int] = dict(pools)
        self._used: dict[str, int] = {}
        self._running = 0

    @property
    def running(self) -> int:
        """Return how many runs hold a slot."""
        return self._running

    @property
    def max_running(self) -> int:
        """Return the global cap."""
        return self._max_running

    def in_use(self, pool: str) -> int:
        """Return how much of ``pool`` is in use."""
        return self._used.get(pool, 0)

    def fits(self, needs: Mapping[str, int], *, global_slot: bool = True) -> bool:
        """Return whether a run needing ``needs`` (and a global slot, if so) could start now."""
        _check_needs(needs)
        if global_slot and self._running >= self._max_running:
            return False
        for pool, amount in needs.items():
            limit = self._pools.get(pool)
            if limit is None or self.in_use(pool) + amount > limit:
                return False
        return True

    def acquire(self, needs: Mapping[str, int], *, global_slot: bool = True) -> None:
        """Take a slot (unless not ``global_slot``) and the pool amounts in ``needs``.

        Raises:
            ValueError: If the run does not fit.

        """
        if not self.fits(needs, global_slot=global_slot):
            msg = f"run needing {dict(needs)} does not fit"
            raise ValueError(msg)
        self._running += int(global_slot)
        for pool, amount in needs.items():
            self._used[pool] = self.in_use(pool) + amount

    def adopt(self, needs: Mapping[str, int], *, global_slot: bool = True) -> None:
        """Take a slot for a run that is already running, even over the limits.

        Recovery uses it for attempts that kept running while the
        scheduler was down: they hold their slots whatever the limits say
        now, just as runs do after a reload lowers a limit.
        """
        _check_needs(needs)
        self._running += int(global_slot)
        for pool, amount in needs.items():
            self._used[pool] = self.in_use(pool) + amount

    def release(self, needs: Mapping[str, int], *, global_slot: bool = True) -> None:
        """Give back a slot (if ``global_slot``) and the pool amounts in ``needs``.

        Raises:
            ValueError: If more is released than was taken.

        """
        _check_needs(needs)
        short = global_slot and self._running < 1
        if short or any(self.in_use(p) < a for p, a in needs.items()):
            msg = f"over-release of {dict(needs)}"
            raise ValueError(msg)
        self._running -= int(global_slot)
        for pool, amount in needs.items():
            self._used[pool] = self.in_use(pool) - amount

    def set_limits(self, max_running: int, pools: Mapping[str, int]) -> None:
        """Swap in new limits; usage may stay over them until runs finish."""
        self._max_running = max_running
        self._pools = dict(pools)


@dataclass(frozen=True)
class _Entry:
    """One queued run with its needs and a version to spot stale heap items."""

    run: Run
    key: tuple[int, float, str]
    needs: Mapping[str, int]
    seq: int
    global_slot: bool = True


def _entry_key(entry: _Entry) -> tuple[int, float, str]:
    """Return the sort key of ``entry``."""
    return entry.key


class ReadyQueue:
    """Hold queued runs and hand out the ones that fit, best first.

    Runs that need a global slot and runs that do not wait in two heaps,
    so a full trampoline never stops a watcher from starting.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._heaps: dict[bool, list[tuple[tuple[int, float, str], int]]] = {True: [], False: []}
        self._live: dict[str, _Entry] = {}
        self._seq = 0

    def __len__(self) -> int:
        """Return the number of queued runs."""
        return len(self._live)

    def __contains__(self, run_id: object) -> bool:
        """Return whether ``run_id`` is queued."""
        return run_id in self._live

    def push(
        self,
        run: Run,
        criticality: Criticality,
        needs: Mapping[str, int],
        *,
        global_slot: bool = True,
    ) -> None:
        """Queue ``run``, replacing any entry with the same run id."""
        self._seq += 1
        key = (-int(criticality), run.scheduled_at.timestamp(), run.run_id)
        self._live[run.run_id] = _Entry(run, key, dict(needs), self._seq, global_slot)
        heapq.heappush(self._heaps[global_slot], (key, self._seq))

    def remove(self, run_id: str) -> bool:
        """Drop ``run_id`` from the queue; return whether it was there."""
        return self._live.pop(run_id, None) is not None

    def take_startable(
        self,
        admission: Admission,
        *,
        force: frozenset[str] = frozenset(),
        limit: int | None = None,
        place: Callable[[Run], bool] | None = None,
    ) -> list[Run]:
        """Start every queued run that fits, best first, acquiring as it goes.

        Runs that do not fit are skipped and stay queued; later runs may
        still start. Run ids in ``force`` go to the front of the line
        but must still fit. With ``limit``, stop once that many have
        started (the rest wait, in order, for the next call). ``place``,
        if given, is asked last about each run that fits; it books what
        the run needs elsewhere and returns False to leave it queued.
        """
        started: list[Run] = []
        room = limit if limit is not None else len(self._live)
        tried: set[str] = set()
        queued = [self._live.get(rid) for rid in force]
        forced: list[_Entry] = sorted((e for e in queued if e is not None), key=_entry_key)
        for entry in forced:
            if len(started) >= room:
                break
            tried.add(entry.run.run_id)
            self._try(entry, admission, started, place)
        for global_slot, heap in self._heaps.items():
            skipped: list[tuple[tuple[int, float, str], int]] = []
            while heap and admission.fits({}, global_slot=global_slot) and len(started) < room:
                key, seq = heapq.heappop(heap)
                entry = self._live.get(key[2])
                if entry is None or entry.seq != seq:
                    continue
                if key[2] in tried or not self._try(entry, admission, started, place):
                    skipped.append((key, seq))
            for item in skipped:
                heapq.heappush(heap, item)
        return started

    def _try(
        self,
        entry: _Entry,
        admission: Admission,
        started: list[Run],
        place: Callable[[Run], bool] | None = None,
    ) -> bool:
        """Start ``entry`` if it fits and ``place`` (if any) agrees; return whether it started."""
        if not admission.fits(entry.needs, global_slot=entry.global_slot):
            return False
        if place is not None and not place(entry.run):
            return False
        admission.acquire(entry.needs, global_slot=entry.global_slot)
        del self._live[entry.run.run_id]
        started.append(entry.run)
        return True
