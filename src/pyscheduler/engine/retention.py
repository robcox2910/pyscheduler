"""Throw away very old notes: pruning history and forgetting old runs.

A school keeps last term's registers in a cupboard, but not those from
ten years ago. Once an hour (and once at start-up) the scheduler clears
out final runs whose run date is older than ``retention``: from the
store, with their attempts, logbook lines and alerts, ``PRUNE_BATCH`` at a
time. Alerts that belong to no run are cleared by their own age.
It also lets go of final runs older than ``memory_days`` in memory
(they stay readable from the store).

Two kinds of run are always kept: open runs, however old, and the final
runs an open run's dependencies still look at (say, last Friday's
``extract`` that tonight's ``report`` waits on next to its other
upstream). Removing those would leave the open run waiting for ever.
"""

from collections.abc import Collection
from datetime import date, datetime, timedelta

import pynet.loop

from pyscheduler.engine.action_support import ActionSupport
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.store import Batch

PRUNE_BATCH = 500
"""How many old runs one pruning commit deletes."""

RETENTION_EVERY = timedelta(hours=1)
"""How often old history is pruned."""


class Retention(ActionSupport):
    """Give the engine pruning and its hourly timer (see the module docstring)."""

    async def prune(self, now: datetime) -> int:
        """Delete final runs dated before the retention cutoff; forget old ones in memory.

        The cutoff is ``now - retention`` as a date in the scheduler's
        time zone (``scheduler.timezone``), the same zone the memory
        window uses. Runs go in batches of ``PRUNE_BATCH``, each with its
        attempts and events, in one commit. A run whose lock is held (an
        action is on it) or that memory has reopened is left for the next
        round. Open runs, and the final runs their dependencies point at,
        are never pruned or forgotten, nor is a run an action holds the
        lock of. The ids deleted are remembered until the next round, so
        a store read that raced the delete (``_load_date``) does not put
        them back in memory. Return how many were deleted.
        """
        settings = self.config.settings
        cutoff = (now.astimezone(settings.timezone) - settings.retention).date()
        keep = self._protected()
        skip: set[str] = set()
        self._recently_deleted.clear()  # a read that raced the last round has landed by now
        deleted = 0
        while not self._stopping:
            ids = await pynet.loop.run_in_thread(
                lambda: self.store.final_run_ids_before(
                    cutoff, PRUNE_BATCH, keep=keep, skip=frozenset(skip)
                )
            )
            locked = [run_id for run_id in ids if self._prunable(run_id)]
            skip.update(run_id for run_id in ids if run_id not in locked)
            try:
                if locked:
                    await self._delete(locked)
            finally:
                for run_id in locked:
                    self._run_locks.give_back(run_id)
            deleted += len(locked)
            if len(ids) < PRUNE_BATCH:
                break
        await self._prune_alerts(now - settings.retention)
        self.book.evict_final_before(
            self._memory_start(now), self._protected(), busy=self._run_locks.busy
        )
        return deleted

    async def _prune_alerts(self, cutoff: datetime) -> None:
        """Delete run-less alerts (and their deliveries) raised before ``cutoff``.

        Alerts of a run go with the run. Scheduler-wide ones (a failed
        reload) have no run, so they are cleared here by age instead.
        """
        while not self._stopping:
            ids = await pynet.loop.run_in_thread(
                lambda: self.store.runless_alert_ids_before(cutoff, PRUNE_BATCH)
            )
            if not ids:
                return
            try:
                await self.writer.save(Batch(deleted_alert_ids=ids))
            except Exception as exc:
                self._fail(exc, ())
                raise EngineFailedError(str(exc)) from exc
            if len(ids) < PRUNE_BATCH:
                return

    def _protected(self) -> Collection[tuple[str, date]]:
        """Return the ``(job, run date)`` slots open runs' dependencies look at.

        Each dependency's date is the open run's date shifted by its
        ``run_date`` offset on the open run's (effective) calendar.
        """
        slots: set[tuple[str, date]] = set()
        for run in self.book.open_runs():
            job = self.config.jobs.get(run.job)
            if job is None:
                continue
            calendar = self.config.calendar_for(job)
            for dep in job.depends_on:
                try:
                    slots.add((dep.job, calendar.shift(run.run_date, dep.run_date_offset)))
                except ValueError:
                    continue
        return frozenset(slots)

    def _prunable(self, run_id: str) -> bool:
        """Lock ``run_id`` if it may be deleted now (final in memory too, and not busy)."""
        run = self.book.get(run_id)
        if run is not None and not run.is_final:
            return False
        return self._run_locks.try_take(run_id)

    async def _delete(self, run_ids: list[str]) -> None:
        """Delete ``run_ids`` durably, then forget them (a failed commit fails the engine)."""
        try:
            await self.writer.save(Batch(deleted_run_ids=run_ids))
        except Exception as exc:
            self._fail(exc, ())
            raise EngineFailedError(str(exc)) from exc
        self._recently_deleted.update(run_ids)  # so a store read racing us does not revive them
        for run_id in run_ids:
            self.book.drop(run_id)

    def _arm_retention(self) -> None:
        """Plan the next pruning round ``RETENTION_EVERY`` from now (unless stopping)."""
        if self._stopping:
            return
        when = self.clock.now() + RETENTION_EVERY
        self._retention_timer = self.clock.call_at(when, self._guard(self._retention_due))

    def _retention_due(self) -> None:
        """Start a pruning round in its own task (it plans the next one when done)."""
        self._retention_timer = None
        self._spawn(self._retention_round(), "engine-retention")

    async def _retention_round(self) -> None:
        """Prune, then plan the next round."""
        try:
            await self.prune(self.clock.now())
        finally:
            self._arm_retention()


async def prune(engine: Retention, now: datetime) -> int:
    """Delete old final runs and forget old ones in memory (see ``Retention.prune``)."""
    return await engine.prune(now)
