"""Operator actions: the buttons a person can press on a run.

Think of a train controller's desk. Trains (runs) mostly follow the
timetable on their own, but the controller can send one round again
(``rerun``), hold one at the platform (``hold`` / ``release``), stop
one (``kill``), wave one through a red signal (``force_start``), or
cross one off the board (``skip``, ``mark_success``, ``mark_failed``).
Adding trains (``trigger``, ``backfill``) and swapping the timetable
(``reload``) live in ``reload``. Every press is written in the logbook
with the controller's name *before* anything moves.

Each action follows one recipe, holding the run's lock throughout (so
it never meets a run halfway through another change): check the engine
is working, check the run may make this move, stage every change with
its event, commit them all in one flush (all or nothing), and only then
act (start, signal).
"""

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import replace

from pyscheduler.engine.action_support import ActionError, ActionSupport
from pyscheduler.engine.creation import downstream_date
from pyscheduler.engine.model import Run, RunState

__all__ = ["ActionError", "OperatorActions"]

_HOLDABLE = frozenset(
    {RunState.SCHEDULED, RunState.WAITING, RunState.BLOCKED, RunState.QUEUED, RunState.RETRY_WAIT}
)
_FORCEABLE = _HOLDABLE | {RunState.HELD}
LOCK_ROUNDS = 100
"""How often a downstream rerun re-plans after meeting a busy run before giving up."""

_REEVALUATED = frozenset({RunState.SCHEDULED, RunState.WAITING, RunState.BLOCKED})


class OperatorActions(ActionSupport):
    """Give the engine its operator actions on runs (see the module docstring)."""

    async def _locked[T](self, run_id: str, body: Callable[[Run], Awaitable[T]]) -> T:
        """Run ``body`` on the run ``run_id`` while holding its lock."""
        async with self._run_lock(run_id):
            return await body(await self._find(run_id))

    # -- rerun -----------------------------------------------------------

    async def rerun(self, run_id: str, *, downstream: bool = False, actor: str) -> list[Run]:
        """Reopen a final run (and, with ``downstream``, its final downstream runs).

        Each reopened run gets ``rerun = attempt`` (a fresh retry budget),
        goes back to ``scheduled`` and is re-evaluated at once. Downstream
        runs are found through ``dependents``: for a dependency with a
        run-date offset, the downstream run date is the one whose offset
        points at the upstream's run date (as for dependency-only runs).
        Only the latest run of each downstream ``(job, run date)`` is
        reopened, and only if it is final; the search goes on through the
        runs it reopens. Downstream jobs that are removed or disabled are
        left alone. Every run reopened is locked first: the rerun waits
        only for the root's lock, and takes the others without waiting (see
        ``_lock_plan``).

        Raises:
            ActionError: 404 unknown run, 409 run not final or job gone or
                disabled (or its downstream runs stay busy), 503 if stopping.

        """
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self._run_lock(run_id))
            run = await self._find(run_id)
            if not run.is_final:
                raise ActionError(409, f"run {run_id} is {run.state.value}, not final")
            self._usable_job(run.job, status=409)
            plan = [run]
            if downstream:
                plan = await self._lock_plan(run, stack)
            now = self.clock.now()
            for current in plan:
                reopened = replace(
                    current,
                    state=RunState.SCHEDULED,
                    reason=f"rerun by {actor}",
                    rerun=current.attempt,
                    held_from=None,
                    updated_at=now,
                )
                self._stage(reopened)
            self._reevaluate(dict.fromkeys(r.job for r in plan))
            return await self._commit(actor, "rerun", {r.run_id: r.state for r in plan})

    async def _lock_plan(self, root: Run, stack: AsyncExitStack) -> list[Run]:
        """Plan the downstream rerun of ``root`` (whose lock is held) and lock all of it.

        The plan and its locks are taken in one step with no waiting, so
        the plan cannot change under it. If a lock is busy, every lock
        taken so far (but the root's) is given back, the busy one is
        waited for on its own, and the plan is made again. Waiting while
        holding only the root (which is upstream of everything planned)
        can never close a circle of waiters.

        Raises:
            ActionError: 409 if the plan stays busy for ``LOCK_ROUNDS`` tries.

        """
        locks = self._run_locks
        for _ in range(LOCK_ROUNDS):
            plan = self._rerun_plan(root)
            taken: list[str] = []
            busy: str | None = None
            for other in plan[1:]:
                if not locks.try_take(other.run_id):
                    busy = other.run_id
                    break
                taken.append(other.run_id)
            if busy is None:
                for run_id in taken:
                    stack.callback(locks.give_back, run_id)
                return plan
            for run_id in taken:
                locks.give_back(run_id)
            await locks.wait_free(busy)
            self._check_usable()
        msg = f"downstream runs of {root.run_id} are busy, try again"
        raise ActionError(409, msg)

    def _rerun_plan(self, root: Run) -> list[Run]:
        """Return ``root`` and the final downstream runs a rerun of it reopens, in order."""
        plan = [root]
        seen = {root.run_id}
        index = 0
        while index < len(plan):
            for found in self._final_downstream(plan[index]):
                if found.run_id not in seen:
                    seen.add(found.run_id)
                    plan.append(found)
            index += 1
        return plan

    def _final_downstream(self, upstream: Run) -> list[Run]:
        """Return the final latest runs of ``upstream``'s dependents for the matching dates."""
        found: list[Run] = []
        for name in sorted(self.config.dependents(upstream.job)):
            job = self.config.jobs.get(name)
            if job is None or not job.enabled:
                continue
            calendar = self.config.calendar_for(job)
            for dep in job.depends_on:
                if dep.job != upstream.job:
                    continue
                day = downstream_date(upstream.run_date, dep.run_date_offset, calendar)
                latest = self.book.latest_for(name, day) if day is not None else None
                if latest is not None and latest.is_final and latest not in found:
                    found.append(latest)
        return found

    # -- hold and release ------------------------------------------------

    async def hold(self, run_id: str, *, actor: str) -> Run:
        """Freeze a run that is scheduled, waiting, blocked, queued or retry-waiting.

        The run leaves the ready queue and its retry timer is cancelled;
        ``held_from`` remembers where it was. Its job's other runs are
        re-checked, since a held run no longer counts for ``overlap``.

        Raises:
            ActionError: 404 unknown run, 409 not holdable, 503 if stopping.

        """

        async def body(run: Run) -> Run:
            """Hold ``run``."""
            if run.state not in _HOLDABLE:
                raise ActionError(409, f"cannot hold run {run_id}: it is {run.state.value}")
            self._unqueue(run_id)
            reason = f"held by {actor}"
            self._stage(replace(self._changed(run, RunState.HELD, reason), held_from=run.state))
            self._reevaluate([run.job])
            return (await self._commit(actor, "hold", {run_id: run.state}))[0]

        return await self._locked(run_id, body)

    async def release(self, run_id: str, *, actor: str) -> Run:
        """Return a held run to ``held_from`` and re-check it at once.

        A run held while scheduled, waiting or blocked is re-evaluated
        (dependencies and overlap). One held while queued goes back
        through the overlap rule into the queue. One held while
        retry-waiting is queued now: the hold counts as its wait.

        Raises:
            ActionError: 404 unknown run, 409 not held (or its job is gone), 503 if stopping.

        """

        async def body(run: Run) -> Run:
            """Release ``run``."""
            if run.state is not RunState.HELD or run.held_from is None:
                raise ActionError(409, f"cannot release run {run_id}: it is {run.state.value}")
            job = self._usable_job(run.job, status=409)
            back = replace(
                self._changed(run, run.held_from, f"released by {actor}"), held_from=None
            )
            self._stage(back)
            if back.state in _REEVALUATED:
                self._reevaluate([run.job])
            else:
                self._reevaluate([run.job, *self._ready(back, job)])
            return (await self._commit(actor, "release", {run_id: run.state}))[0]

        return await self._locked(run_id, body)

    # -- kill and force-start --------------------------------------------

    async def kill(self, run_id: str, *, actor: str) -> Run:
        """Stop a running attempt: SIGTERM now, SIGKILL after ``kill_grace``; no retry.

        The kill is recorded first, then signalled. The run's lock keeps
        the attempt's ending out until the kill flag is set, so the ending
        always sees it. An attempt still being launched (no handle yet) is
        signalled as soon as it has one. The run ends ``failed`` with
        reason ``killed by <actor>``.

        Raises:
            ActionError: 404 unknown run, 409 not running or already being
                killed, 503 if stopping.

        """

        async def body(run: Run) -> Run:
            """Kill ``run``'s attempt."""
            if run.state is not RunState.RUNNING:
                raise ActionError(409, f"cannot kill run {run_id}: it is {run.state.value}")
            if run_id in self._kill_requested:
                raise ActionError(409, f"run {run_id} is already being killed")
            result = (await self._commit(actor, "kill", {run_id: run.state}))[0]
            self._kill_requested.add(run_id)
            self._kill_actors[run_id] = actor
            handle = self._handles.get(run_id)
            if handle is not None:
                self._signal_kill(run_id, handle)
            return result

        return await self._locked(run_id, body)

    async def force_start(self, run_id: str, *, actor: str) -> Run:
        """Queue a run now, first in line, ignoring its dependencies and ``overlap``.

        Pools and the global cap still apply. Allowed from scheduled,
        waiting, blocked, held, retry_wait, and queued (to jump the line).

        Raises:
            ActionError: 404 unknown run, 409 not allowed (or its job is gone), 503 if stopping.

        """

        async def body(run: Run) -> Run:
            """Force ``run`` into the queue."""
            if run.state not in _FORCEABLE:
                msg = f"cannot force-start run {run_id}: it is {run.state.value}"
                raise ActionError(409, msg)
            job = self._usable_job(run.job, status=409)
            self._unqueue(run_id)
            queued = replace(
                self._changed(run, RunState.QUEUED, f"force-started by {actor}"), held_from=None
            )
            self._stage(queued)
            self._enqueue(queued, job)
            self._force.add(run_id)
            return (await self._commit(actor, "force_start", {run_id: run.state}))[0]

        return await self._locked(run_id, body)

    # -- ending by hand --------------------------------------------------

    async def skip(self, run_id: str, *, actor: str) -> Run:
        """End an open, not running run as skipped ("pretend it worked")."""
        return await self._end(run_id, RunState.SKIPPED, "skip", actor, final_ok=False)

    async def mark_success(self, run_id: str, *, actor: str) -> Run:
        """Set a run that is not running to succeeded (final runs included)."""
        return await self._end(run_id, RunState.SUCCEEDED, "mark_success", actor, final_ok=True)

    async def mark_failed(self, run_id: str, *, actor: str) -> Run:
        """Set a run that is not running to failed (final runs included)."""
        return await self._end(run_id, RunState.FAILED, "mark_failed", actor, final_ok=True)

    async def _end(
        self, run_id: str, state: RunState, action: str, actor: str, *, final_ok: bool
    ) -> Run:
        """Set ``state`` by hand, drop the run's queue entry and timer, re-check dependents.

        Raises:
            ActionError: 404 unknown run, 409 running (or final, unless
                ``final_ok``), 503 if stopping.

        """

        async def body(run: Run) -> Run:
            """End ``run`` by hand."""
            if run.state is RunState.RUNNING or (run.is_final and not final_ok):
                msg = f"cannot {action} run {run_id}: it is {run.state.value}"
                raise ActionError(409, msg)
            self._unqueue(run_id)
            ended = replace(self._changed(run, state, f"{action} by {actor}"), held_from=None)
            self._stage(ended)
            self._reevaluate([run.job])
            return (await self._commit(actor, action, {run_id: run.state}))[0]

        return await self._locked(run_id, body)
