"""Run the scheduler as a service: config, store, engine and HTTP API on one loop.

Opening a shop takes the same few steps every morning, in the same
order: check the delivery note (the config), unlock the stockroom (the
store), wake the staff (the engine), and only then turn the sign on the
door to OPEN (the HTTP server). Closing is the reverse: turn the sign
round, let the customers inside finish, then send the staff home. Jobs
already running are not interrupted: their minders carry on, and the
engine adopts them again on the next start.

``serve`` does exactly that. Signals from the operating system steer it:

- ``SIGTERM`` or ``SIGINT`` (Ctrl-C): close gracefully;
- ``SIGHUP``: reload the config files (problems go to the log).

Example::

    import pynet.loop
    from pathlib import Path
    from pyscheduler.service import serve

    pynet.loop.run(serve(Path("scheduler.yaml"), ready=print))
"""

import ipaddress
import json
import logging
import signal
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import FrameType
from typing import Any

import pynet.loop
from pynet.loop import Event, Task
from pyweb.aserver import AsyncServer

from pyscheduler.api import Reload, TokenFileError, TokenTable, build_app, config_actions
from pyscheduler.config import Config, load_config
from pyscheduler.engine.action_support import ActionError
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import MinderLauncher
from pyscheduler.engine.model import Run, RunState
from pyscheduler.store import Batch, ConfigRecord, Store

__all__ = ["CLOSE_GRACE", "SchedulerFailedError", "ServiceError", "is_loopback", "serve"]

logger = logging.getLogger("pyscheduler.service")

CLOSE_GRACE = 5.0
"""How many seconds in-flight API requests get to finish when the service closes."""

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
"""The signals that end ``serve`` gracefully."""

RELOAD_SIGNAL = signal.SIGHUP
"""The signal that reloads the config files."""

type _Handler = Callable[[int, FrameType | None], Any] | int | None


class ServiceError(Exception):
    """Raised when ``serve`` refuses to start (unsafe address, bad tokens file, busy port)."""


class SchedulerFailedError(Exception):
    """Raised by ``serve`` when the engine failed stop (a store commit failed)."""


def is_loopback(host: str) -> bool:
    """Return whether ``host`` only accepts connections from this machine.

    ``localhost``, ``127.0.0.0/8`` and ``::1`` (with or without brackets)
    count; every other name or address does not.
    """
    bare = host.strip().removeprefix("[").removesuffix("]")
    if bare.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def _tokens(config: Config) -> TokenTable | None:
    """Return the token table, or ``None`` for development mode on a loopback address.

    Raises:
        ServiceError: If there is no tokens file and the address is not
            loopback, or the tokens file cannot be read.

    """
    settings = config.settings
    host, _ = settings.listen
    if settings.api_tokens_file is None:
        if not is_loopback(host):
            msg = (
                f"refusing to listen on {host} without api_tokens_file: development mode "
                "(no tokens) is only allowed on a loopback address such as 127.0.0.1"
            )
            raise ServiceError(msg)
        return None
    try:
        return TokenTable.load(settings.api_tokens_file)
    except TokenFileError as exc:
        raise ServiceError(str(exc)) from None


def _record(config: Config, version: int, engine: Engine) -> ConfigRecord:
    """Return the config record for ``config`` as ``version``."""
    sources = json.dumps(dict(config.sources), sort_keys=True)
    return ConfigRecord(version, engine.clock.now(), config.digest, sources)


async def _start_engine(config: Config) -> tuple[Engine, ConfigRecord | None]:
    """Open the store and start the engine; return it and the config record still to save.

    The version is the stored one when the digest is unchanged, else the
    next one. The record is saved only after ``start``, so catch-up still
    compares against the previous config's jobs.

    Raises:
        SchedulerFailedError: If recovery or catch-up failed.

    """
    store = await pynet.loop.run_in_thread(Store.open, config.settings.state_dir)
    try:
        latest = await pynet.loop.run_in_thread(store.latest_config)
    except BaseException:
        await pynet.loop.run_in_thread(store.close)
        raise
    changed = latest is None or latest.digest != config.digest
    version = 1 if latest is None else latest.version + int(changed)
    engine = Engine(
        config, store, clock=SystemClock(), launcher=MinderLauncher(), config_version=version
    )
    engine.subscribe(_RunLog())
    try:
        await engine.start()
    except EngineFailedError as exc:
        await engine.stop()
        raise SchedulerFailedError(str(exc)) from exc
    except BaseException:
        await engine.stop()
        raise
    return engine, _record(config, version, engine) if changed else None


class _Signals:
    """Hook SIGTERM, SIGINT and SIGHUP onto the loop while ``serve`` runs (main thread only)."""

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


class _RunLog:
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


async def _reload_for_signal(reload: Reload) -> None:
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


async def _until(aw: Coroutine[Any, Any, object], wake: Event) -> None:
    """Await ``aw``, then set ``wake``."""
    await aw
    wake.set()


async def serve(
    config_path: Path,
    *,
    ready: Callable[[int], None] | None = None,
    stop: Event | None = None,
) -> None:
    """Run the scheduler for the config at ``config_path`` until told to stop.

    It loads the config, opens the store, starts the engine, records the
    config version (when its digest is new), then serves the API on
    ``settings.listen`` (port 0 picks a free port). ``ready(port)`` is
    called once it listens. It ends when ``stop`` is set, on SIGTERM or
    SIGINT, or when the engine fails; SIGHUP reloads the config. Signals
    are only hooked when called on the main thread.

    Shutting down closes the server (letting requests finish for
    ``CLOSE_GRACE`` seconds) and stops the engine without killing running
    attempts: their minders carry on and are adopted on the next start.

    Raises:
        ConfigError: If the config has errors (nothing has been opened).
        ServiceError: If it refuses to start (see ``ServiceError``).
        StoreSchemaError: If the state folder holds another store layout.
        SchedulerFailedError: If the engine failed stop.

    """
    config = await pynet.loop.run_in_thread(load_config, config_path)
    tokens = _tokens(config)
    host, port = config.settings.listen
    bind = host.removeprefix("[").removesuffix("]")
    engine, record = await _start_engine(config)
    server: AsyncServer | None = None
    signals: _Signals | None = None
    watchers: list[Task[None]] = []
    reloads: set[Task[None]] = set()
    try:
        if record is not None:
            await engine.writer.save(Batch(configs=[record]))
        reload, validate = config_actions(engine, config_path)
        server = AsyncServer(build_app(engine, tokens=tokens, reload=reload, validate=validate))
        try:
            await server.start(bind, port)
        except OSError as exc:
            msg = f"cannot listen on {host}:{port}: {exc}"
            raise ServiceError(msg) from None
        logger.info("scheduler %s listening on %s:%d", config.settings.name, host, server.port)
        wake = Event()

        def start_reload() -> None:
            """Start a reload task for SIGHUP."""
            task = pynet.loop.spawn(_reload_for_signal(reload), name="sighup-reload")
            reloads.add(task)
            task.add_done_callback(lambda done: reloads.discard(done))

        signals = _Signals(wake.set, start_reload)
        signals.install()
        watchers.append(pynet.loop.spawn(_until(engine.wait_failed(), wake), name="failed"))
        if stop is not None:
            watchers.append(pynet.loop.spawn(_until(stop.wait(), wake), name="stop"))
        if ready is not None:
            ready(server.port)
        await wake.wait()
    finally:
        await _shut_down(engine, server, signals, watchers, reloads)
    if engine.error is not None:
        raise SchedulerFailedError(str(engine.error))


async def _shut_down(
    engine: Engine,
    server: AsyncServer | None,
    signals: _Signals | None,
    watchers: list[Task[None]],
    reloads: set[Task[None]],
) -> None:
    """Restore the signals, close the server, let reloads end, then stop the engine."""
    if signals is not None:
        signals.restore()
    for task in watchers:
        task.cancel()
    try:
        if server is not None:
            await server.close(CLOSE_GRACE)
        if reloads:
            await pynet.loop.gather(*reloads, return_exceptions=True)
    finally:
        await engine.stop()
        logger.info("scheduler stopped")
