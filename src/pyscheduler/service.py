"""Run the scheduler as a service: config, store, engine and HTTP API on one loop.

Opening a shop takes the same few steps every morning, in the same
order: check the delivery note (the config), unlock the stockroom (the
store), wake the staff (the engine), and only then turn the sign on the
door to OPEN (the HTTP server). The door is unlocked early (the port is
taken first, so a busy port is found before any job starts) but the sign
says "starting" until the staff are ready. Closing is the reverse: turn the sign
round, let the customers inside finish, then send the staff home. Jobs
already running are not interrupted: their minders carry on, and the
engine adopts them again on the next start.

``serve`` does exactly that. Signals from the operating system steer it:

- ``SIGTERM`` or ``SIGINT`` (Ctrl-C): close gracefully;
- ``SIGHUP``: reload the config files, the tokens file and the users
  file (problems go to the log).

Example::

    import pynet.loop
    from pathlib import Path
    from pyscheduler.service import serve

    pynet.loop.run(serve(Path("scheduler.yaml"), ready=print))
"""

import ipaddress
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pynet.loop
from pynet.errors import PyNetError
from pynet.loop import Event, Task
from pyweb.aserver import AsyncServer

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.alerts.monitor import AlertMonitor
from pyscheduler.api import (
    TokenFileError,
    TokenHolder,
    TokenTable,
    UserHolder,
    UsersFileError,
    UserTable,
    build_app,
    config_actions,
)
from pyscheduler.api.access import needs_session_key
from pyscheduler.api.session_key import SessionKeyError, load_or_make_key
from pyscheduler.config import Config, load_config
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import MinderLauncher
from pyscheduler.service_parts import (
    RunLog,
    SignalHooks,
    StartingGate,
    logger,
    reload_for_signal,
)
from pyscheduler.store import Batch, ConfigRecord, Store

__all__ = ["CLOSE_GRACE", "SchedulerFailedError", "ServiceError", "is_loopback", "serve"]

CLOSE_GRACE = 5.0
"""How many seconds in-flight API requests get to finish when the service closes."""


class ServiceError(Exception):
    """Raised when ``serve`` refuses to start (unsafe address, bad tokens or users, busy port)."""


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


def _access(config: Config) -> tuple[TokenTable | None, UserTable | None]:
    """Return the token table and the users table (``None`` for a file not set).

    With neither file the scheduler is in development mode, which is only
    allowed on a loopback address.

    Raises:
        ServiceError: If there is neither file and the address is not
            loopback, or a file cannot be read.

    """
    settings = config.settings
    host, _ = settings.listen
    if settings.api_tokens_file is None and settings.users_file is None:
        if not is_loopback(host):
            msg = (
                f"refusing to listen on {host} without api_tokens_file or users_file: "
                "development mode (no tokens, no users) is only allowed on a loopback "
                "address such as 127.0.0.1"
            )
            raise ServiceError(msg)
        return None, None
    try:
        tokens = (
            None if settings.api_tokens_file is None else TokenTable.load(settings.api_tokens_file)
        )
        users = None if settings.users_file is None else UserTable.load(settings.users_file)
    except (TokenFileError, UsersFileError) as exc:
        raise ServiceError(str(exc)) from None
    return tokens, users


async def _open_engine(config: Config) -> tuple[Engine, ConfigRecord | None]:
    """Open the store and build (not start) the engine; return it and the record to save.

    The version is the stored one when the digest is unchanged, else the
    next one. The record is saved only after ``start``, so catch-up still
    compares against the previous config's jobs.
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
    engine.subscribe(RunLog())
    if not changed:
        return engine, None
    sources = json.dumps(dict(config.sources), sort_keys=True)
    return engine, ConfigRecord(version, engine.clock.now(), config.digest, sources)


async def _start(engine: Engine, record: ConfigRecord | None) -> None:
    """Start the engine, then save the config record.

    Raises:
        SchedulerFailedError: If recovery or catch-up failed, or the
            record could not be saved.

    """
    try:
        await engine.start()
    except EngineFailedError as exc:
        raise SchedulerFailedError(str(exc)) from exc
    if record is None:
        return
    try:
        await engine.writer.save(Batch(configs=[record]))
    except Exception as exc:
        logger.exception("recording config version %d failed", record.version)
        msg = f"cannot record config version {record.version}: {exc}"
        raise SchedulerFailedError(msg) from exc


async def _until(aw: Coroutine[Any, Any, object], wake: Event) -> None:
    """Await ``aw``, then set ``wake``."""
    await aw
    wake.set()


class _Service:
    """Hold what one ``serve`` call has opened, so shutting down can undo it."""

    def __init__(self) -> None:
        """Start with nothing open."""
        self.wake = Event()
        self.gate = StartingGate()
        self.engine: Engine | None = None
        self.dispatcher: Dispatcher | None = None
        self.monitor: AlertMonitor | None = None
        self.server: AsyncServer | None = None
        self.signals: SignalHooks | None = None
        self.watchers: list[Task[None]] = []
        self.reloads: set[Task[None]] = set()
        self.reload_task: Callable[[], Coroutine[Any, Any, None]] | None = None

    def on_reload(self) -> None:
        """Start a reload for SIGHUP (ignored until the engine is up)."""
        if not self.gate.open or self.reload_task is None:
            logger.warning("SIGHUP ignored: the scheduler is still starting")
            return
        task = pynet.loop.spawn(self.reload_task(), name="sighup-reload")
        self.reloads.add(task)
        task.add_done_callback(lambda done: self.reloads.discard(done))

    def watch(self, aw: Coroutine[Any, Any, object], name: str) -> None:
        """Wake ``serve`` when ``aw`` finishes."""
        self.watchers.append(pynet.loop.spawn(_until(aw, self.wake), name=name))

    async def shut_down(self) -> None:
        """Close the server, let reloads end, stop the alerts and the engine, then restore signals.

        The dispatcher stops first: alerts raised after that stay
        ``pending`` in the store and are sent at the next start. The
        monitor keeps listening while the engine winds down (a run can
        still fail then) and is stopped by ``Engine.stop`` just before
        the writer closes.

        The signal handlers go last, so a second Ctrl-C while closing is
        a no-op rather than a ``KeyboardInterrupt``.
        """
        for task in self.watchers:
            task.cancel()
        try:
            if self.server is not None:
                await self.server.close(CLOSE_GRACE)
            if self.reloads:
                await pynet.loop.gather(*self.reloads, return_exceptions=True)
        finally:
            try:
                await self._stop_alerts()
            finally:
                try:
                    if self.engine is not None:
                        await self.engine.stop()
                        logger.info("scheduler stopped")
                finally:
                    if self.signals is not None:
                        self.signals.restore()

    async def _stop_alerts(self) -> None:
        """Stop the dispatcher (safe if never started); the monitor stops with the engine."""
        if self.dispatcher is not None:
            await self.dispatcher.stop()


async def serve(
    config_path: Path,
    *,
    ready: Callable[[int], None] | None = None,
    stop: Event | None = None,
) -> None:
    """Run the scheduler for the config at ``config_path`` until told to stop.

    In order: load the config; read the tokens and users; hook the signals; open
    the store; take the port (requests get 503 ``scheduler is starting``
    for now); start the engine; record the config version (when its
    digest is new); start the alert dispatcher and monitor; open the gate
    and call ``ready(port)``. It ends when ``stop`` is set, on SIGTERM or
    SIGINT (also during start-up: it stops once the engine is up), or
    when the engine fails. SIGHUP reloads the config (a reload that finds
    errors raises a ``reload_failed`` alert). Signals are only hooked on
    the main thread.

    Shutting down closes the server (letting requests finish for
    ``CLOSE_GRACE`` seconds), stops the alert dispatcher, then stops the
    engine without killing running attempts: their minders carry on and
    are adopted on the next start. The alert monitor stops with the
    engine, just before its writer closes.

    Raises:
        ConfigError: If the config has errors (nothing has been opened).
        ServiceError: If it refuses to start (see ``ServiceError``).
        StoreSchemaError: If the state folder holds another store layout.
        PyDBError: If the store cannot be opened (another scheduler has it).
        OSError: If the state folder cannot be made.
        SchedulerFailedError: If the engine failed stop.

    """
    config = await pynet.loop.run_in_thread(load_config, config_path)
    token_table, user_table = await pynet.loop.run_in_thread(_access, config)
    tokens, users = TokenHolder(token_table), UserHolder(user_table)
    host, port = config.settings.listen
    service = _Service()
    service.signals = SignalHooks(service.wake.set, service.on_reload)
    service.signals.install()
    try:
        engine, record = await _open_engine(config)
        service.engine = engine
        dispatcher = service.dispatcher = Dispatcher(engine)
        monitor = service.monitor = AlertMonitor(engine, dispatcher)  # hears recovery too
        reload, validate = config_actions(
            engine, config_path, tokens=tokens, users=users, on_failed=monitor.reload_failed
        )
        service.reload_task = lambda: reload_for_signal(reload)
        try:  # the key file is read (or made) off the loop; the app is built on it
            key = None
            if needs_session_key(tokens, users):
                key = await pynet.loop.run_in_thread(load_or_make_key, config.settings.state_dir)
            app = build_app(
                engine,
                tokens=tokens,
                reload=reload,
                validate=validate,
                dispatcher=dispatcher,
                users=users,
                secure=not is_loopback(host),
                session_key=key,
            )
        except (SessionKeyError, OSError) as exc:
            raise ServiceError(str(exc)) from None
        app.middleware(service.gate.middleware())
        service.server = AsyncServer(app)
        try:
            await service.server.start(host.removeprefix("[").removesuffix("]"), port)
        except (PyNetError, OSError) as exc:
            cause = exc.__cause__ if isinstance(exc, PyNetError) and exc.__cause__ else exc
            msg = f"cannot listen on {host}:{port}: {cause}"
            raise ServiceError(msg) from None
        await _start(engine, record)
        await dispatcher.start()
        await monitor.start()
        if not service.wake.is_set():  # a stop signal while starting skips the rest
            service.watch(engine.wait_failed(), "failed")
            if stop is not None:
                service.watch(stop.wait(), "stop")
            service.gate.open = True
            logger.info(
                "scheduler %s listening on %s:%d", config.settings.name, host, service.server.port
            )
            if ready is not None:
                ready(service.server.port)
            await service.wake.wait()
    finally:
        await service.shut_down()
    if engine.error is not None:
        raise SchedulerFailedError(str(engine.error))
