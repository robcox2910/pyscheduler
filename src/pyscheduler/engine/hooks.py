"""Listeners: who wants to hear when a run changes, or an attempt starts or ends.

Run subscribers hear each run once its change is durable (the API's live
view and the service's run log listen there).

Like the bell at a boxing match, which rings at the start of a round and
again at its end, the engine calls every attempt listener twice per
attempt: once when the attempt's ``running`` row is durable, and once when
its ending is. The alert monitor listens here to time slow attempts and
notice short or lost ones.

Reload listeners are called once a reload's new config is in force (and
durable), so anything timed from the old config can be set again.

Closing listeners are awaited by ``Engine.stop`` once its attempts have
wound down but before the store writer closes: the last moment anything
can still be written. The alert monitor stops there, so a run that ends
while the scheduler stops still gets its alert stored.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable

from pyscheduler.engine.model import Attempt, Run

logger = logging.getLogger("pyscheduler.engine")


class AttemptHooks:
    """Own the run, attempt and reload listeners of the engine (``EngineBase`` inherits this)."""

    def __init__(self) -> None:
        """Start with no listeners."""
        self._subscribers: list[Callable[[Run], None]] = []
        self._attempt_listeners: list[Callable[[Attempt], None]] = []
        self._reload_listeners: list[Callable[[], None]] = []
        self._closing_listeners: list[Callable[[], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[Run], None]) -> Callable[[], None]:
        """Call ``callback`` with each run once its change is durable; return an unsubscribe."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            """Stop calling ``callback`` (a second call does nothing)."""
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

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

    def watch_reloads(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` after each reload that changed the config; return an unsubscribe.

        A listener that raises is logged and skipped.
        """
        self._reload_listeners.append(callback)

        def unsubscribe() -> None:
            """Stop calling ``callback`` (a second call does nothing)."""
            if callback in self._reload_listeners:
                self._reload_listeners.remove(callback)

        return unsubscribe

    def before_close(self, callback: Callable[[], Awaitable[None]]) -> Callable[[], None]:
        """Await ``callback`` in ``Engine.stop`` just before the writer closes; return an unsubscribe.

        By then the engine starts nothing more and its attempts have wound
        down, but the writer still saves. A listener that raises is logged
        and skipped.
        """
        self._closing_listeners.append(callback)

        def unsubscribe() -> None:
            """Stop awaiting ``callback`` (a second call does nothing)."""
            if callback in self._closing_listeners:
                self._closing_listeners.remove(callback)

        return unsubscribe

    async def _notify_closing(self) -> None:
        """Await every closing listener in turn; one that raises is logged and skipped."""
        for callback in list(self._closing_listeners):
            try:
                await callback()
            except Exception:
                logger.exception("closing listener failed")

    def _notify(self, run: Run) -> None:
        """Tell every subscriber about ``run``; one that raises is logged and skipped."""
        for callback in list(self._subscribers):
            try:
                callback(run)
            except Exception:
                logger.exception("subscriber failed for %s", run.run_id)

    def _notify_reloaded(self) -> None:
        """Tell every reload listener; one that raises is logged and skipped."""
        for callback in list(self._reload_listeners):
            try:
                callback()
            except Exception:
                logger.exception("reload listener failed")

    def _notify_attempts(self, attempts: Iterable[Attempt]) -> None:
        """Tell every listener about each attempt; one that raises is logged and skipped."""
        for attempt in attempts:
            for callback in list(self._attempt_listeners):
                try:
                    callback(attempt)
                except Exception:
                    logger.exception("attempt listener failed for %s", attempt.run_id)
