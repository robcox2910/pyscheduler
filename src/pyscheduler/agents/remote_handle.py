"""The scheduler's handle on an attempt running on a remote agent.

A local attempt's handle watches a process. A remote one has no process
here to watch; it is more like a parcel-tracking number. The hub fills in
the tracking page as the agent's letters arrive: ``started`` gives the
log paths on the agent's disk, ``finished`` gives how it ended. If the
agent goes quiet for too long, the hub stamps the parcel *lost*.
"""

from collections.abc import Callable
from datetime import datetime

from pynet.loop import Event

from pyscheduler.agents.messages import RunKey
from pyscheduler.engine.status_note import AttemptResult

__all__ = ["RemoteHandle"]


class RemoteHandle:
    """An ``AttemptHandle`` for an attempt on agent ``agent``; the hub resolves it.

    Args:
        agent: The agent running the attempt.
        run_id: The run's id.
        attempt: The attempt number.
        signal: Called with the handle and ``"TERM"`` or ``"KILL"`` to send a kill;
            it must not block (the hub sends the message on a task).
        started_at: When the attempt started, if known (adopted attempts).

    Attributes:
        lost: True once the hub gave up on the attempt (no report in time).
        stdout_path: The agent-side stdout path, once ``started`` arrives.
        stderr_path: The agent-side stderr path, once ``started`` arrives.

    """

    def __init__(
        self,
        agent: str,
        run_id: str,
        attempt: int,
        *,
        signal: Callable[[RemoteHandle, str], None],
        started_at: datetime | None = None,
    ) -> None:
        """Start unresolved, with no log paths."""
        self.agent = agent
        self.run_id = run_id
        self.attempt = attempt
        self.started_at = started_at
        self.lost = False
        self.stdout_path: str | None = None
        self.stderr_path: str | None = None
        self._signal = signal
        self._ended = Event()
        self._result: AttemptResult | None = None

    def __repr__(self) -> str:
        """Show which attempt on which agent."""
        return f"RemoteHandle(agent={self.agent!r}, run_id={self.run_id!r}, attempt={self.attempt})"

    @property
    def key(self) -> RunKey:
        """Return ``(run_id, attempt)``."""
        return self.run_id, self.attempt

    @property
    def pid(self) -> int | None:
        """Return ``None``: the process lives on another machine."""
        return None

    @property
    def done(self) -> bool:
        """Return whether the attempt has been resolved (finished or lost)."""
        return self._result is not None

    async def wait(self) -> AttemptResult:
        """Wait for the hub to resolve the attempt and return how it ended."""
        while self._result is None:
            await self._ended.wait()
        return self._result

    def terminate(self) -> None:
        """Ask the agent to send SIGTERM to the attempt (does nothing once resolved)."""
        if not self.done:
            self._signal(self, "TERM")

    def kill(self) -> None:
        """Ask the agent to send SIGKILL to the attempt (does nothing once resolved)."""
        if not self.done:
            self._signal(self, "KILL")

    def record_start(self, stdout_path: str, stderr_path: str) -> bool:
        """Note the agent-side log paths; return whether this is the first ``started``."""
        first = self.stdout_path is None
        self.stdout_path, self.stderr_path = stdout_path, stderr_path
        return first

    def resolve(self, result: AttemptResult, *, lost: bool = False) -> None:
        """Settle the attempt with ``result`` (the first call wins), waking every waiter."""
        if self._result is not None:
            return
        self._result = result
        self.lost = lost
        self._ended.set()
