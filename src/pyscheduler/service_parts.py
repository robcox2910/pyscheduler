"""The small parts ``serve`` is built from: signal hooks, the starting gate and the run log.

- ``SignalHooks`` turns SIGTERM/SIGINT/SIGHUP into callbacks on the loop.
- ``StartingGate`` answers every request 503 until the engine is up, so
  the port can be taken (and a busy port found) before recovery and
  catch-up launch anything.
- ``RunLog`` writes one log line each time a run changes state.
- ``reload_for_signal`` reloads the config for SIGHUP and logs what
  went wrong instead of raising.
"""

import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

import pynet.loop
from pyweb.app import CallNext, Middleware
from pyweb.messages import HttpRequest, Outcome, json

from pyscheduler.api import Reload
from pyscheduler.engine.action_support import ActionError
from pyscheduler.engine.model import Run, RunState

__all__ = [
    "RELOAD_SIGNAL",
    "STOP_SIGNALS",
    "RunLog",
    "ServiceError",
    "SignalHooks",
    "StartingGate",
    "logger",
    "reload_for_signal",
]

logger = logging.getLogger("pyscheduler.service")


class ServiceError(Exception):
    """Raised when ``serve`` refuses to start (unsafe address, bad tokens or users, busy port)."""


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
"""The signals that end ``serve`` gracefully."""

RELOAD_SIGNAL = signal.SIGHUP
"""The signal that reloads the config files."""

UNAVAILABLE = 503

type _Handler = Callable[[int, FrameType | None], Any] | int | None


class SignalHooks:
    """Hook SIGTERM, SIGINT and SIGHUP onto the running loop (main thread only)."""

    def __init__(self, on_stop: Callable[[], None], on_reload: Callable[[], None]) -> None:
        """Remember what each signal does; nothing is hooked until ``install``."""
        self._loop = pynet.loop.get_running_loop()
        self._on_stop = on_stop
        self._on_reload = on_reload
        self._previous: dict[int, _Handler] = {}

    def install(self) -> None:
        """Install the signal handlers, keeping the old ones; do nothing off the main thread."""
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (*STOP_SIGNALS, RELOAD_SIGNAL):
            self._previous[signum] = signal.signal(signum, self._handle)

    def restore(self) -> None:
        """Put the previous handlers back."""
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        """Pass the signal to the loop (a signal handler must not touch loop state itself)."""
        del frame
        action = self._on_reload if signum == RELOAD_SIGNAL else self._on_stop

        def on_loop() -> None:
            """Log the signal and act on it, on the loop (logging is not signal-safe)."""
            logger.info("received %s", signal.Signals(signum).name)
            action()

        self._loop.call_soon_threadsafe(on_loop)


class StartingGate:
    """Answer 503 ``scheduler is starting`` until ``open`` is set."""

    def __init__(self) -> None:
        """Start closed."""
        self.open = False

    def middleware(self) -> Middleware:
        """Return the middleware that holds requests back while the gate is closed."""

        async def gate(request: HttpRequest, call_next: CallNext) -> Outcome:
            """Refuse while starting; otherwise go on."""
            if not self.open:
                return json({"error": "scheduler is starting"}, UNAVAILABLE)
            return await call_next(request)

        return gate


class RunLog:
    """Log one line each time a run changes state (the engine tells its subscribers)."""

    def __init__(self) -> None:
        """Start with no runs seen."""
        self._states: dict[str, RunState] = {}

    def __call__(self, run: Run) -> None:
        """Log ``run`` if its state changed; forget it once it is final."""
        if self._states.get(run.run_id) is run.state:
            return
        reason = f": {run.reason}" if run.reason else ""
        level = logging.WARNING if run.state is RunState.FAILED else logging.INFO
        logger.log(level, "run %s %s (attempt %d)%s", run.run_id, run.state, run.attempt, reason)
        if run.is_final:
            self._states.pop(run.run_id, None)
        else:
            self._states[run.run_id] = run.state


async def reload_for_signal(reload: Reload) -> None:
    """Reload the config files for ``SIGHUP``, logging what went wrong instead of raising."""
    try:
        problems = await reload("signal")
    except ActionError as exc:
        logger.error("reload failed: %s", exc)  # noqa: TRY400 -- the reason is the message
        return
    errors = [p for p in problems if p.severity == "error"]
    for problem in problems:
        log = logger.error if problem.severity == "error" else logger.warning
        log("config: %s", problem)
    if errors:
        logger.error("reload failed: the config has %d error(s); nothing changed", len(errors))
    else:
        logger.info("config reloaded")
