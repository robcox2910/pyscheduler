"""Remote runs in the engine: place a run on an agent, send it there, and follow it.

A job with ``agent:`` or ``agent_tags:`` is like a parcel for a courier
firm. Before it leaves the line, the dispatcher books a courier with room
(``Placement.choose`` and ``reserve``, in one step, so two parcels never
take one seat). If no courier fits, the parcel stays in line with a note
saying why ("waiting for agent build-01 (busy)"), and the line is looked
at again whenever a courier turns up or frees a seat. The parcel goes out
with its address filled in (the rendered command), and its tracking page
(a ``RemoteHandle``) says where its logs are once it has ``started`` and
how it ended once it has ``finished``, or that it was *lost*.

After a scheduler restart, a parcel recorded as out with a courier
(status path ``agent:<name>``) is sent again if its job is still a remote
one (the agent's journal knows it, so it never runs twice), or else just
followed (``adopt``).

The hub owns a booked seat once the work is sent: it frees it when the
attempt finishes or is lost. The engine frees a seat itself only for an
attempt that ends without ever being sent.
"""

import logging
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from pynet.loop import Task
from pynet.messaging import JsonValue

from pyscheduler.agents.book import AgentState, Refusal, RunKey
from pyscheduler.agents.log_messages import LogReply
from pyscheduler.agents.messages import WorkOrder, work_payload
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.config import JobDef
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.launcher import AttemptHandle, AttemptResult
from pyscheduler.engine.model import Attempt, Run, RunState
from pyscheduler.engine.outcomes import Watch
from pyscheduler.engine.watch_runs import WatchRuns
from pyscheduler.templates import render

__all__ = [
    "NO_PLACEMENT",
    "REMOTE_PREFIX",
    "SHELL",
    "Placement",
    "RemoteRuns",
    "is_remote",
    "remote_agent",
]

SHELL = ("/bin/sh", "-c")
"""How a command written as one string is run."""

REMOTE_PREFIX = "agent:"
"""What a remote attempt's status path starts with; the agent's name follows (P4-R5)."""

NO_PLACEMENT = "waiting for agents: none are set up"
"""The queued reason of an agent job when the engine has no placement (no hub)."""

logger = logging.getLogger("pyscheduler.engine.remote")


class Placement(Protocol):
    """Where remote runs go: choose and hold an agent slot, send work, follow attempts.

    ``AgentHub`` provides it through ``HubPlacement``; tests use ``FakeHub``.
    """

    def choose(self, job: JobDef) -> str | Refusal:
        """Return the agent to run ``job`` on now, or a ``Refusal`` with the queued reason."""
        ...

    def reserve(self, agent: str, key: RunKey) -> None:
        """Hold a slot on ``agent`` for attempt ``key`` (idempotent)."""
        ...

    def release(self, agent: str, key: RunKey) -> None:
        """Free the slot ``key`` holds on ``agent`` (idempotent)."""
        ...

    def watch(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` whenever capacity may have freed; return the unsubscribe."""
        ...

    def watch_started(self, callback: Callable[[RemoteHandle], None]) -> Callable[[], None]:
        """Call ``callback`` with a handle when its attempt first starts; return the unsubscribe."""
        ...

    def dispatch(self, agent: str, payload: Mapping[str, JsonValue]) -> RemoteHandle:
        """Send a work payload to ``agent`` and return the handle that follows it."""
        ...

    def adopt(self, agent: str, run_id: str, attempt: int, started_at: datetime) -> RemoteHandle:
        """Follow an attempt found running on ``agent`` after a restart."""
        ...

    def recovered(self) -> None:
        """Note that recovery has picked up every remote attempt it will follow."""
        ...

    def states(self) -> list[AgentState]:
        """Return a copy of every agent's state, by name (for the API and the UI)."""
        ...

    async def fetch_log(
        self,
        agent: str,
        run_id: str,
        attempt: int,
        stream: str,
        *,
        mode: str,
        count: int | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> LogReply:
        """Ask ``agent`` for part of an attempt's log (see ``AgentHub.fetch_log``)."""
        ...


def is_remote(job: JobDef) -> bool:
    """Return whether ``job`` runs on an agent (it names one, or tags one must have)."""
    return job.agent is not None or bool(job.agent_tags)


def remote_agent(row: Attempt) -> str | None:
    """Return the agent a remote attempt row ran on, or ``None`` for a local one."""
    if row.status_path.startswith(REMOTE_PREFIX):
        return row.status_path.removeprefix(REMOTE_PREFIX)
    return None


def _with_paths(row: Attempt, handle: RemoteHandle) -> Attempt:
    """Return ``row`` with the agent-side log paths ``handle`` knows (if any)."""
    return replace(
        row,
        stdout_path=handle.stdout_path or row.stdout_path,
        stderr_path=handle.stderr_path or row.stderr_path,
    )


class RemoteRuns(WatchRuns):
    """Give the engine remote runs: placement, work orders, following and re-adoption."""

    placement: Placement | None
    """Where remote runs go, or ``None`` when agents are off (set up by ``Engine``)."""
    _placed: dict[str, tuple[str, RunKey]]
    """Slots the engine booked for runs whose work has not been sent yet."""
    _remote_rows: dict[str, Attempt]
    """The attempt row of each remote attempt being followed, by run id."""
    _awaiting_agent: set[str]
    """Queued runs that no agent fitted at their last look."""

    def _setup_remote(self, placement: Placement | None) -> None:
        """Start with nothing placed, and listen to ``placement`` (if any)."""
        self.placement = placement
        self._placed = {}
        self._remote_rows = {}
        self._awaiting_agent = set()
        if placement is not None:
            placement.watch(self._agents_changed)
            placement.watch_started(self._remote_started)

    # -- placement at admission ------------------------------------------

    def _place(self, run: Run) -> bool:
        """Book an agent slot for ``run`` if its job is remote; return whether it may start.

        Called by the ready queue once the global cap and pools fit, so a
        refused run holds no slot. A refusal becomes the run's queued reason.
        """
        job = self.config.jobs.get(run.job)
        if job is None or not is_remote(job):
            return True
        if self.placement is None:
            return self._waiting(run.run_id, NO_PLACEMENT)
        choice = self.placement.choose(job)
        if isinstance(choice, Refusal):
            return self._waiting(run.run_id, choice.reason)
        current = self.book.get(run.run_id) or run
        key = (run.run_id, current.attempt + 1)
        self.placement.reserve(choice, key)
        self._placed[run.run_id] = (choice, key)
        self._awaiting_agent.discard(run.run_id)
        return True

    def _waiting(self, run_id: str, reason: str) -> bool:
        """Show ``reason`` on the queued run ``run_id`` (if it changed); return False."""
        self._awaiting_agent.add(run_id)
        current = self.book.get(run_id)
        if current is not None and current.state is RunState.QUEUED and current.reason != reason:
            self._stage(self._changed(current, RunState.QUEUED, reason))
        return False

    def _agents_changed(self) -> None:
        """Look at the queue again soon if a run is waiting for an agent."""
        self._awaiting_agent &= {run_id for run_id in self._awaiting_agent if run_id in self.ready}
        if self._awaiting_agent and not self._stopping:
            self._flush_soon()  # the flush ends with a pump

    def _release(self, run_id: str) -> None:
        """Give back ``run_id``'s slots, and its agent slot if its work was never sent."""
        super()._release(run_id)
        placed = self._placed.pop(run_id, None)
        if placed is not None and self.placement is not None:
            self.placement.release(*placed)

    def _drop_placements(self) -> None:
        """Free every agent slot booked for work that was never sent (at stop)."""
        for run_id in list(self._placed):
            self._release(run_id)

    def _remote_status(self, run_id: str) -> str | None:
        """Return the status path of ``run_id``'s attempt if it was placed on an agent."""
        placed = self._placed.get(run_id)
        return None if placed is None else f"{REMOTE_PREFIX}{placed[0]}"

    # -- the work order ----------------------------------------------------

    def _command(self, run: Run, job: JobDef) -> tuple[tuple[str, ...], str | None, dict[str, str]]:
        """Render ``run``'s argv, cwd and env (the job's own env and ``PYSCHED_*`` only).

        Raises:
            KeyError: If a template names an unknown value.
            ValueError: If a template is malformed.

        """
        values = self._values(run, job)
        if isinstance(job.command, str):
            argv = (*SHELL, render(job.command, values))
        else:
            argv = tuple(render(word, values) for word in job.command)
        env = {
            **{key: render(value, values) for key, value in job.env.items()},
            "PYSCHED_JOB": job.name,
            "PYSCHED_RUN_DATE": run.run_date.isoformat(),
            "PYSCHED_RUN_ID": run.run_id,
            "PYSCHED_ATTEMPT": str(run.attempt),
        }
        files = self._upstream_files(run, job)
        if files is not None:
            env["PYSCHED_FILES"] = "\n".join(files)
        return argv, None if job.cwd is None else render(job.cwd, values), env

    def _work_order(self, run: Run, job: JobDef) -> WorkOrder:
        """Return the work order of ``run``'s current attempt (P4-R6).

        Raises:
            KeyError: If a template names an unknown value.
            ValueError: If a template is malformed.

        """
        argv, cwd, env = self._command(run, job)
        return WorkOrder(
            run_id=run.run_id,
            attempt=run.attempt,
            job=job.name,
            run_date=run.run_date.isoformat(),
            argv=argv,
            cwd=cwd,
            env=env,
        )

    # -- sending and following ---------------------------------------------

    def _dispatch(self, row: Attempt, order: WorkOrder) -> RemoteHandle:
        """Send ``order`` to the agent ``row`` names; the hub owns the slot from now on.

        Raises:
            RuntimeError: If there is no placement, or the hub has stopped.
            ValueError: If the hub refuses the payload.

        """
        agent = remote_agent(row)
        if self.placement is None or agent is None:
            msg = "no agent hub to send the work to"
            raise RuntimeError(msg)
        handle = self.placement.dispatch(agent, work_payload(order))
        self._placed.pop(row.run_id, None)
        self._track(row, handle)
        return handle

    def _track(self, row: Attempt, handle: RemoteHandle) -> None:
        """Note ``handle`` (and its row) as the attempt of ``row``'s run."""
        self._handles[row.run_id] = handle
        self._remote_rows.setdefault(row.run_id, row)

    async def _follow_remote(
        self, run: Run, row: Attempt, handle: RemoteHandle, started: datetime | None = None
    ) -> None:
        """Wait for a remote attempt to end (timeout and kills armed), then record it."""
        run_id = run.run_id
        self._track(row, handle)
        if run_id in self._kill_requested:  # an operator kill came while it was sent
            self._signal_kill(run_id, handle)
        watch = self._arm(run, handle, started)
        try:
            result = await self._idle_while(handle)
        finally:
            watch.cancel()
            self._handles.pop(run_id, None)
            row = self._remote_rows.pop(run_id, row)
        watch.note(handle)  # a lost attempt ends as lost
        await self._finish(_with_paths(row, handle), result, watch)

    def _remote_started(self, handle: RemoteHandle) -> None:
        """Write the agent-side log paths on the attempt row (in a task) once it has started."""
        if not self._stopping:
            self._spawn(self._note_started(handle), f"started-{handle.run_id}")

    async def _note_started(self, handle: RemoteHandle) -> None:
        """Commit ``handle``'s log paths on its row, unless its attempt has ended meanwhile.

        The run's lock keeps this commit and the attempt's ending in order.
        """
        run_id = handle.run_id
        async with self._run_lock(run_id):
            row = self._remote_rows.get(run_id)
            if row is None or self._handles.get(run_id) is not handle:
                return
            row = self._remote_rows[run_id] = _with_paths(row, handle)
            await self._transition([], attempts=[row])

    # -- recovery ------------------------------------------------------------

    def _pick_up_remote(self, run: Run, job: JobDef | None, row: Attempt, agent: str) -> Task[None]:
        """Send again or adopt a remote attempt found running at start; return its task.

        Its job still remote: the work is sent again (the agent's journal
        runs it once, and it covers work a restarted broker lost). Otherwise
        the attempt is adopted. With no placement it cannot be followed, so
        it is lost.
        """
        handle: RemoteHandle | None = None
        try:
            handle = self._follow_again(run, job, row, agent)
        except (RuntimeError, ValueError) as exc:
            logger.warning("cannot follow %s on agent %s again: %s", run.run_id, agent, exc)
        if handle is None:
            lost = AttemptResult(None, None, None, self.clock.now(), None)
            return self._spawn(self._finish(row, lost, Watch(lost=True)), f"recover-{run.run_id}")
        self._track(row, handle)
        return self._spawn(self._resume_remote(run, row, handle), f"adopt-{run.run_id}")

    def _follow_again(
        self, run: Run, job: JobDef | None, row: Attempt, agent: str
    ) -> RemoteHandle | None:
        """Return a handle on ``row``'s attempt from the placement, or ``None`` if there is none."""
        if self.placement is None:
            return None
        if job is not None and is_remote(job):
            try:
                order = self._work_order(run, job)
            except KeyError, ValueError:  # it cannot be rendered now: follow it as it is
                pass
            else:
                return self.placement.dispatch(agent, work_payload(order))
        return self.placement.adopt(agent, run.run_id, row.attempt, row.started_at)

    async def _resume_remote(self, run: Run, row: Attempt, handle: RemoteHandle) -> None:
        """Log the adoption, then follow the attempt, its timeout counted from its start."""
        adopted = self._event(
            run.run_id, "attempt_adopted", attempt=row.attempt, agent=handle.agent
        )
        try:
            await self._transition([], events=[adopted])
        except EngineFailedError:
            self._handles.pop(run.run_id, None)
            self._remote_rows.pop(run.run_id, None)
            raise
        await self._follow_remote(run, row, handle, row.started_at)

    # -- parts provided by the attempt runner ------------------------------

    @abstractmethod
    def _arm(self, run: Run, handle: AttemptHandle, started: datetime | None = None) -> Watch:
        """Arm the job's timeout from ``started`` (default now)."""

    @abstractmethod
    async def _idle_while(self, handle: AttemptHandle) -> AttemptResult:
        """Wait for ``handle`` without counting as busy."""

    @abstractmethod
    async def _finish(self, row: Attempt, result: AttemptResult, watch: Watch) -> None:
        """Free the slots, record how the attempt ended, then retry or end the run."""
