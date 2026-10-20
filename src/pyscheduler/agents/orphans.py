"""Stop work an agent still runs after the scheduler gave up on it.

When the hub loses an attempt it sends one ``TERM``, sealed there and
then. An agent that comes back more than ``MAX_SKEW`` later refuses that
letter as stale, and carries on running the job. Its next ``hello`` or
``heartbeat`` still lists the attempt, though, so the hub can tell: the
attempt has no tracking page (no ``RemoteHandle``) any more.

Think of a foreman who called a crew off a site by post, but the letter
went out of date in the mail. When the crew's next postcard says they
are still digging, the foreman writes again (a fresh letter: ``TERM``),
and if they are still at it after the usual grace, a firmer one
(``KILL``). He does not write on every postcard, only once per round.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from pyscheduler.agents.messages import RunKey
from pyscheduler.engine.clock import Cancel, Clock

__all__ = ["OrphanReaper"]

type Signal = Callable[[str, RunKey, str], None]
"""Send ``kill`` with a signal (``TERM`` or ``KILL``) for an attempt on an agent."""


@dataclass
class _Round:
    """One round of stopping an orphan: when the next may start, and the pending ``KILL``."""

    next_round: datetime
    kill: Cancel | None


class OrphanReaper:
    """Send a fresh ``TERM``, then ``KILL`` after the grace, to orphaned attempts.

    An orphan is an attempt an agent reports running that the hub no
    longer follows. Each one gets at most one round (``TERM`` now,
    ``KILL`` after ``grace``) per two graces, however often it is
    reported; one the agent stops listing is forgotten, and its pending
    ``KILL`` is not sent.

    Args:
        clock: The engine clock that times the ``KILL`` and the rounds.
        grace: How long a ``TERM`` has before ``KILL`` (the ``kill_grace``).
        signal: Sends one ``kill`` message, sealed afresh.

    """

    def __init__(self, *, clock: Clock, grace: timedelta, signal: Signal) -> None:
        """Start with no orphans."""
        self._clock = clock
        self._grace = grace
        self._signal = signal
        self._rounds: dict[tuple[str, RunKey], _Round] = {}

    def seen(self, agent: str, orphans: set[RunKey]) -> None:
        """Note that ``agent`` reports running ``orphans`` (and no other orphans)."""
        for agent_key in [k for k in self._rounds if k[0] == agent and k[1] not in orphans]:
            self._forget(agent_key)
        now = self._clock.now()
        for key in sorted(orphans):
            known = self._rounds.get((agent, key))
            if known is not None and now < known.next_round:
                continue
            if known is not None and known.kill is not None:
                known.kill.cancel()
            self._signal(agent, key, "TERM")
            kill = self._clock.call_at(now + self._grace, partial(self._kill, (agent, key)))
            self._rounds[agent, key] = _Round(now + 2 * self._grace, kill)

    def _kill(self, agent_key: tuple[str, RunKey]) -> None:
        """Send ``KILL`` for an orphan whose ``TERM`` grace ran out."""
        known = self._rounds.get(agent_key)
        if known is None or known.kill is None:
            return
        known.kill = None
        self._signal(agent_key[0], agent_key[1], "KILL")

    def _forget(self, agent_key: tuple[str, RunKey]) -> None:
        """Drop an orphan and cancel its pending ``KILL``."""
        known = self._rounds.pop(agent_key, None)
        if known is not None and known.kill is not None:
            known.kill.cancel()

    def close(self) -> None:
        """Forget every orphan and cancel every pending ``KILL``."""
        for agent_key in list(self._rounds):
            self._forget(agent_key)
