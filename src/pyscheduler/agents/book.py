"""Remember which agents are online, how busy they are, and who fits a job.

Think of the book as the front-desk sign-in sheet at a workshop. Each
worker (agent) signs in whenever it speaks, and the desk notes how many
jobs it is holding. If a worker is silent for too long, the desk crosses
it off as gone. When a job needs a worker, the desk looks down the sheet
and picks the one who is in, has a free pair of hands, and is the least
busy.

All timing goes through the injected engine ``Clock``.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from pyscheduler.engine.clock import Cancel, Clock

RunKey = tuple[str, int]
"""A run id and an attempt number: the key of one slot held on an agent."""


@dataclass(frozen=True)
class Refusal:
    """No agent fits; ``reason`` is the text shown on the queued run."""

    reason: str


@dataclass
class AgentState:
    """What the book knows about one agent."""

    name: str
    tags: frozenset[str] = frozenset()
    max_running: int = 0
    running: set[RunKey] = field(default_factory=set[RunKey])
    online: bool = False
    last_seen: datetime | None = None
    went_offline: datetime | None = None


def _copy(state: AgentState) -> AgentState:
    """Return a copy of ``state`` that does not share its running set."""
    return replace(state, running=set(state.running))


class AgentBook:
    """Track agents and choose one for a job.

    Args:
        clock: The engine clock that arms the offline timers.
        grace: How long an agent may stay silent before it counts as offline.

    """

    def __init__(self, *, clock: Clock, grace: timedelta) -> None:
        """Start with an empty book."""
        self._clock = clock
        self._grace = grace
        self._agents: dict[str, AgentState] = {}
        self._timers: dict[str, Cancel] = {}
        self._watchers: list[Callable[[AgentState], None]] = []

    def heard(
        self,
        name: str,
        *,
        tags: Iterable[str] | None = None,
        max_running: int | None = None,
        running: Iterable[RunKey] | None = None,
    ) -> None:
        """Record that ``name`` spoke: mark it online and re-arm its offline timer.

        Arguments left as ``None`` keep the stored values. ``running``
        replaces the set, because the agent is the authority on what it runs.
        """
        state = self._agents.setdefault(name, AgentState(name))
        before = (state.online, state.tags, state.max_running, set(state.running))
        now = self._clock.now()
        state.online = True
        state.last_seen = now
        if tags is not None:
            state.tags = frozenset(tags)
        if max_running is not None:
            state.max_running = max_running
        if running is not None:
            state.running = set(running)
        timer = self._timers.pop(name, None)
        if timer is not None:
            timer.cancel()
        self._timers[name] = self._clock.call_at(now + self._grace, lambda: self._expire(name))
        if before != (state.online, state.tags, state.max_running, state.running):
            self._notify(state)

    def _expire(self, name: str) -> None:
        """Mark ``name`` offline: its grace ran out."""
        self._timers.pop(name, None)
        state = self._agents[name]
        if not state.online:
            return
        state.online = False
        state.went_offline = self._clock.now()
        self._notify(state)

    def choose(self, *, agent: str | None, tags: frozenset[str]) -> str | Refusal:
        """Pick an agent for a job, or say why none fits.

        Args:
            agent: The named agent the job insists on, or ``None``.
            tags: Tags an agent must all have when no name is given.

        Returns:
            The chosen agent's name, or a ``Refusal`` with the queued reason.

        """
        if agent is not None:
            state = self._agents.get(agent)
            if state is None or not state.online:
                return Refusal(f"waiting for agent {agent} (offline)")
            if not self._has_room(state):
                return Refusal(f"waiting for agent {agent} (busy)")
            return agent
        fits = [
            s for s in self._agents.values() if s.online and tags <= s.tags and self._has_room(s)
        ]
        if not fits:
            return Refusal(f"waiting for an agent with tags {', '.join(sorted(tags))}")
        return min(fits, key=lambda s: (len(s.running), s.name)).name

    @staticmethod
    def _has_room(state: AgentState) -> bool:
        """Return whether ``state`` has a free slot."""
        return len(state.running) < state.max_running

    def reserve(self, name: str, key: RunKey) -> None:
        """Hold a slot on ``name`` for ``key`` (idempotent)."""
        state = self._agents.get(name)
        if state is None or key in state.running:
            return
        state.running.add(key)
        self._notify(state)

    def release(self, name: str, key: RunKey) -> None:
        """Free the slot ``key`` held on ``name``; do nothing if it held none."""
        state = self._agents.get(name)
        if state is None or key not in state.running:
            return
        state.running.discard(key)
        self._notify(state)

    def states(self) -> list[AgentState]:
        """Return copies of every agent's state, sorted by name."""
        return [_copy(self._agents[name]) for name in sorted(self._agents)]

    def watch(self, callback: Callable[[AgentState], None]) -> Callable[[], None]:
        """Call ``callback`` with a copy of the state whenever an agent changes.

        Returns:
            A function that stops the watching.

        """
        self._watchers.append(callback)

        def stop() -> None:
            """Remove the callback if it is still registered."""
            if callback in self._watchers:
                self._watchers.remove(callback)

        return stop

    def _notify(self, state: AgentState) -> None:
        """Tell every watcher about ``state``."""
        for callback in list(self._watchers):
            callback(_copy(state))

    def close(self) -> None:
        """Cancel every offline timer."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
