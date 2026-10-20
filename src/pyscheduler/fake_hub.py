"""A pretend agent hub for tests: agents that connect, go quiet and finish on a fake clock.

``FakeHub`` plays the scheduler's agent hub and its agents at once, so the
engine's remote runs can be tested without a broker. It lives beside
``pyscheduler.testing`` (whose ``FakeResult`` plans its attempts).
"""

import signal
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from pynet.messaging import JsonValue

from pyscheduler.agents.book import AgentBook, AgentState, Refusal, RunKey
from pyscheduler.agents.hub_link import AgentOffline
from pyscheduler.agents.log_messages import LogReply
from pyscheduler.agents.messages import parse_key
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.config import JobDef
from pyscheduler.engine.clock import Cancel, FakeClock
from pyscheduler.engine.launcher import AttemptResult
from pyscheduler.testing import FakeResult

__all__ = ["AGENT_GRACE", "LOST_AFTER", "FakeHub"]


AGENT_GRACE = timedelta(seconds=60)
"""How long a ``FakeHub`` agent may stay silent before it counts as offline."""

LOST_AFTER = timedelta(minutes=10)
"""How long after going offline a ``FakeHub`` agent's attempts are lost."""

DEFAULT_MAX_RUNNING = 4
"""How many attempts a ``FakeHub`` agent runs at once unless told otherwise."""


@dataclass
class _Duty:
    """One attempt in a fake agent's journal: what it runs and how it ended."""

    agent: str
    plan: FakeResult
    started_at: datetime
    result: AttemptResult | None = None
    timer: Cancel | None = None


class FakeHub:
    """Pretend to be the agent hub and its agents, all on a ``FakeClock``.

    It places jobs with a real ``AgentBook`` and hands out real
    ``RemoteHandle``s, so the engine sees the hub's own reasons and slots.
    The agents are scripted: ``connect`` brings one online (it keeps
    talking every half grace), ``disconnect`` silences it (offline after
    the grace, and its attempts lost ``lost_after`` later unless it comes
    back first). Each attempt follows its job's ``plan`` like
    ``FakeLauncher``, and a repeated dispatch of an attempt the agent
    already holds runs nothing new (the agent journal).

    Attributes:
        book: The agent book that chooses, reserves and releases slots.
        dispatches: How often each ``(run_id, attempt)`` was dispatched.
        payloads: Every work payload dispatched, in order.
        runs: How often each attempt really started at an agent.
        adopted: The attempts followed through ``adopt``, in order.
        signals: Each ``(key, "TERM"|"KILL")`` sent to an agent, in order.
        recovered_calls: How often ``recovered`` was called.
        logs: The scripted log bytes of each ``(run_id, attempt, stream)``.
        silent: Agents that are online but never answer a log request.
        log_requests: How many log requests were made.
        stopped: While true, ``dispatch`` and ``adopt`` raise ``RuntimeError``.

    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        grace: timedelta = AGENT_GRACE,
        lost_after: timedelta = LOST_AFTER,
    ) -> None:
        """Start with no agents and nothing followed."""
        self.clock = clock
        self.grace, self.lost_after = grace, lost_after
        self.book = AgentBook(clock=clock, grace=grace)
        self.dispatches: Counter[RunKey] = Counter()
        self.payloads: list[Mapping[str, JsonValue]] = []
        self.runs: Counter[RunKey] = Counter()
        self.adopted: list[RunKey] = []
        self.signals: list[tuple[RunKey, str]] = []
        self.recovered_calls = 0
        self.logs: dict[tuple[str, int, str], bytes] = {}
        self.silent: set[str] = set()
        self.log_requests = 0
        self.stopped = False
        self._profiles: dict[str, tuple[frozenset[str], int]] = {}
        self._talking: dict[str, Cancel] = {}
        self._journal: dict[RunKey, _Duty] = {}
        self._handles: dict[RunKey, RemoteHandle] = {}
        self._lost: dict[RunKey, Cancel] = {}
        self._plans: dict[str, tuple[FakeResult, ...]] = {}
        self._counts: Counter[str] = Counter()
        self._started: list[Callable[[RemoteHandle], None]] = []
        self.book.watch(self._on_agent)

    # Scripting ---------------------------------------------------------

    def plan(self, job: str, *results: FakeResult) -> None:
        """Set how ``job``'s attempts go at the agents (as ``FakeLauncher.plan``)."""
        self._plans[job] = results

    def connect(
        self, name: str, *, tags: Iterable[str] = (), max_running: int = DEFAULT_MAX_RUNNING
    ) -> None:
        """Bring ``name`` online; it reports every finish it holds and keeps talking."""
        self._profiles[name] = (frozenset(tags), max_running)
        self._talk(name)
        for key, handle in list(self._handles.items()):
            if handle.agent == name:
                self._report(key)

    def disconnect(self, name: str) -> None:
        """Silence ``name``: offline after the grace, its attempts lost after ``lost_after``."""
        timer = self._talking.pop(name, None)
        if timer is not None:
            timer.cancel()

    def online(self, name: str) -> bool:
        """Return whether the book has ``name`` online."""
        return any(s.online for s in self.book.states() if s.name == name)

    def restarted(self, clock: FakeClock) -> FakeHub:
        """Return a new hub on ``clock`` with the same agents and their journals.

        It plays a scheduler restart: the hub forgets what it followed, but
        the agents keep running their attempts and reconnect at once.
        """
        hub = FakeHub(clock, grace=self.grace, lost_after=self.lost_after)
        hub._plans, hub._counts = self._plans, self._counts
        hub.dispatches, hub.runs, hub.payloads = self.dispatches, self.runs, self.payloads
        for key, duty in self._journal.items():
            hub._journal[key] = duty
            if duty.result is None:
                hub._finish_at(key, duty)
        for name in self._talking:
            tags, max_running = self._profiles[name]
            hub.connect(name, tags=tags, max_running=max_running)
        return hub

    # The placement surface ----------------------------------------------

    def choose(self, job: JobDef) -> str | Refusal:
        """Pick an agent for ``job`` (as the hub's book does)."""
        return self.book.choose(agent=job.agent, tags=frozenset(job.agent_tags))

    def reserve(self, agent: str, key: RunKey) -> None:
        """Hold a slot on ``agent`` for ``key``."""
        self.book.reserve(agent, key)

    def release(self, agent: str, key: RunKey) -> None:
        """Free the slot ``key`` holds on ``agent``."""
        self.book.release(agent, key)

    def watch(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` whenever an agent changes; return the unsubscribe."""
        return self.book.watch(lambda _state: callback())

    def watch_started(self, callback: Callable[[RemoteHandle], None]) -> Callable[[], None]:
        """Call ``callback`` with a handle when its attempt first starts."""
        self._started.append(callback)
        return lambda: self._started.remove(callback)

    def recovered(self) -> None:
        """Count the call."""
        self.recovered_calls += 1

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
        """Answer a log request from the scripted ``logs``, as an agent would.

        Raises:
            AgentOffline: If ``agent`` is not online.
            TimeoutError: If ``agent`` is in ``silent``.

        """
        self.log_requests += 1
        if not self.online(agent):
            raise AgentOffline(agent)
        if agent in self.silent:
            raise TimeoutError
        data = self.logs.get((run_id, attempt, stream))
        if data is None:
            return LogReply("fake", missing=True)
        if mode == "tail":
            lines = data.decode("utf-8", errors="replace").removesuffix("\n").split("\n")
            wanted = lines[-(count or 1) :] if data else []
            return LogReply(
                "fake", lines=wanted, truncated=len(wanted) < len(lines), offset=len(data)
            )
        start, size = offset or 0, len(data)
        shrank = size < start
        begin = 0 if shrank else start
        chunk = data[begin : begin + (limit or 0)]
        return LogReply("fake", data=chunk, offset=begin + len(chunk), shrank=shrank)

    def dispatch(self, agent: str, payload: Mapping[str, JsonValue]) -> RemoteHandle:
        """Send ``payload`` to ``agent``; it starts there unless the agent already has it.

        Raises:
            RuntimeError: If ``stopped``.

        """
        key = parse_key(payload)
        self.dispatches[key] += 1
        self.payloads.append(payload)
        handle = self._follow(agent, key, None)
        if key not in self._journal and agent in self._talking:
            job = str(payload["job"])
            results = self._plans.get(job) or (FakeResult(),)
            plan = results[min(self._counts[job], len(results) - 1)]
            self._counts[job] += 1
            self.runs[key] += 1
            self._journal[key] = duty = _Duty(agent, plan, self.clock.now())
            if plan.start_error is not None:
                duty.result = AttemptResult(None, None, None, self.clock.now(), plan.start_error)
            else:
                self._finish_at(key, duty)
        self._report(key)
        return handle

    def adopt(self, agent: str, run_id: str, attempt: int, started_at: datetime) -> RemoteHandle:
        """Follow an attempt a restarted scheduler found on ``agent``.

        Raises:
            RuntimeError: If ``stopped``.

        """
        key = (run_id, attempt)
        self.adopted.append(key)
        handle = self._follow(agent, key, started_at)
        self._report(key)
        return handle

    # Inside -------------------------------------------------------------

    def _follow(self, agent: str, key: RunKey, started_at: datetime | None) -> RemoteHandle:
        """Return the handle of ``key``, making and reserving it (with a countdown) if new."""
        if self.stopped:
            msg = "the hub has stopped"
            raise RuntimeError(msg)
        if (known := self._handles.get(key)) is not None:
            return known
        handle = RemoteHandle(agent, *key, signal=self._signal, started_at=started_at)
        self._handles[key] = handle
        self.book.reserve(agent, key)
        self._arm_lost(key, self.clock.now() + self.lost_after)
        return handle

    def _talk(self, name: str) -> None:
        """Have ``name`` speak now and again in half a grace."""
        tags, max_running = self._profiles[name]
        self.book.heard(name, tags=tags, max_running=max_running)
        when = self.clock.now() + self.grace / 2
        self._talking[name] = self.clock.call_at(when, lambda: self._talk(name))

    def _finish_at(self, key: RunKey, duty: _Duty) -> None:
        """End ``key``'s attempt at the agent when its planned duration is up."""
        plan = duty.plan
        when = duty.started_at + plan.duration
        duty.timer = self.clock.call_at(when, lambda: self._end(key, plan.exit_code, plan.signal))

    def _end(self, key: RunKey, exit_code: int | None, signum: int | None) -> None:
        """Record how ``key`` ended at its agent, and report it if the agent is talking."""
        duty = self._journal[key]
        if duty.result is not None:
            return
        if duty.timer is not None:
            duty.timer.cancel()
        duty.result = AttemptResult(exit_code, signum, duty.started_at, self.clock.now(), None)
        self._report(key)

    def _report(self, key: RunKey) -> None:
        """If ``key``'s agent is talking, send what it knows: started, then finished."""
        handle, duty = self._handles.get(key), self._journal.get(key)
        if handle is None or duty is None or duty.agent not in self._talking:
            return
        self._quiet(key)
        out = f"/agent/{duty.agent}/{key[0]}.{key[1]}"
        if handle.record_start(f"{out}.out", f"{out}.err"):
            for callback in list(self._started):
                callback(handle)
        if duty.result is not None:
            self._settle(handle, duty.result, lost=False)

    def _signal(self, handle: RemoteHandle, signal_name: str) -> None:
        """Pass a kill to the agent, which ends the attempt (TERM unless it ignores TERM)."""
        self.signals.append((handle.key, signal_name))
        duty = self._journal.get(handle.key)
        if duty is None or duty.agent not in self._talking:
            return
        if signal_name == "KILL":
            self._end(handle.key, None, int(signal.SIGKILL))
        elif not duty.plan.ignores_term:
            self._end(handle.key, None, int(signal.SIGTERM))

    def _on_agent(self, state: AgentState) -> None:
        """When an agent goes offline, count down to losing its attempts."""
        if state.online or state.went_offline is None:
            return
        for key, handle in self._handles.items():
            if handle.agent == state.name and key not in self._lost:
                self._arm_lost(key, state.went_offline + self.lost_after)

    def _arm_lost(self, key: RunKey, when: datetime) -> None:
        """Lose ``key`` at ``when`` unless its agent reports first."""
        self._quiet(key)
        self._lost[key] = self.clock.call_at(when, lambda: self._lose(key))

    def _quiet(self, key: RunKey) -> None:
        """Stop ``key``'s lost countdown."""
        timer = self._lost.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _lose(self, key: RunKey) -> None:
        """Resolve ``key`` as lost."""
        self._lost.pop(key, None)
        handle = self._handles.get(key)
        if handle is not None:
            self._settle(handle, AttemptResult(None, None, None, self.clock.now(), None), lost=True)

    def _settle(self, handle: RemoteHandle, result: AttemptResult, *, lost: bool) -> None:
        """Stop following ``handle``, free its slot and resolve it."""
        self._quiet(handle.key)
        self._handles.pop(handle.key, None)
        self.book.release(handle.agent, handle.key)
        handle.resolve(result, lost=lost)
