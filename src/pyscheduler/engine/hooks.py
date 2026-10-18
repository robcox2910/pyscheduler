"""Attempt listeners: who wants to hear when an attempt starts or ends.

Like the bell at a boxing match, which rings at the start of a round and
again at its end, the engine calls every attempt listener twice per
attempt: once when the attempt's ``running`` row is durable, and once when
its ending is. The alert monitor listens here to time slow attempts and
notice short or lost ones.
"""

import logging
from collections.abc import Callable, Iterable

from pyscheduler.engine.model import Attempt

logger = logging.getLogger("pyscheduler.engine")


class AttemptHooks:
    """Own the attempt listeners of the engine (``EngineBase`` inherits this)."""

    def __init__(self) -> None:
        """Start with no listeners."""
        self._attempt_listeners: list[Callable[[Attempt], None]] = []

    def watch_attempts(self, callback: Callable[[Attempt], None]) -> Callable[[], None]:
        """Call ``callback`` with each attempt row once it is durable; return an unsubscribe.

        The rows are those committed together with a run change: the start
        (``ended_at`` is ``None``) and the ending. A later note about the
        same started attempt (its pid, or an adoption after a restart) is
        not repeated. A listener that raises is logged and skipped, as
        with ``subscribe``.
        """
        self._attempt_listeners.append(callback)

        def unsubscribe() -> None:
            """Stop calling ``callback`` (a second call does nothing)."""
            if callback in self._attempt_listeners:
                self._attempt_listeners.remove(callback)

        return unsubscribe

    def _notify_attempts(self, attempts: Iterable[Attempt]) -> None:
        """Tell every listener about each attempt; one that raises is logged and skipped."""
        for attempt in attempts:
            for callback in list(self._attempt_listeners):
                try:
                    callback(attempt)
                except Exception:
                    logger.exception("attempt listener failed for %s", attempt.run_id)
