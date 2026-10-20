"""The admin endpoints: health, reload and validate.

``reload`` and ``validate`` are handed in as callables, so the API does
not need to know where the config files live. ``config_actions`` makes
the usual pair: both read the files on disk on a helper thread;
``reload`` then swaps the new config into the engine (and re-reads the
tokens file and the users file, so a revoked token or a removed user
stops working, and the agents' secrets), ``validate`` only reports.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse, json

from pyscheduler.api.auth import HEALTH_PATH, TokenFileError, TokenHolder, TokenTable
from pyscheduler.api.serialize import problem_json
from pyscheduler.api.support import BAD_REQUEST, ApiError, actor_of, guarded
from pyscheduler.api.users import UserHolder, UsersFileError, UserTable
from pyscheduler.config import Config, ConfigError, Problem, load_config
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.config.model import AgentsDef
from pyscheduler.engine.core import Engine

__all__ = ["Reload", "Validate", "add_admin_routes", "config_actions"]

type JsonObject = dict[str, JsonValue]

type Reload = Callable[[str], Awaitable[list[Problem]]]
"""Reload the config for the named actor; return its problems (errors mean nothing changed)."""

type Validate = Callable[[], Awaitable[list[Problem]]]
"""Check the config files on disk without loading them; return their problems."""

UNAVAILABLE = 503


def _reread_tokens(
    config: Config, path: Path, holder: TokenHolder
) -> tuple[TokenTable | None, list[Problem]]:
    """Read the new config's tokens file; return the table or the problem with it."""
    tokens_file = config.settings.api_tokens_file
    if (tokens_file is None) != (holder.table is None):
        message = "api_tokens_file: switching between development mode and tokens needs a restart"
        return None, [Problem(str(path), 1, message)]
    if tokens_file is None:
        return None, []
    try:
        return TokenTable.load(tokens_file), []
    except TokenFileError as exc:
        return None, [Problem(str(path), 1, f"api_tokens_file: {exc}")]


def _reread_users(
    config: Config, path: Path, holder: UserHolder
) -> tuple[UserTable | None, list[Problem]]:
    """Read the new config's users file; return the table or the problem with it."""
    users_file = config.settings.users_file
    if (users_file is None) != (holder.table is None):
        message = "users_file: switching the web UI on or off needs a restart"
        return None, [Problem(str(path), 1, message)]
    if users_file is None:
        return None, []
    try:
        return UserTable.load(users_file), []
    except UsersFileError as exc:
        return None, [Problem(str(path), 1, f"users_file: {exc}")]


_RESTART_FIELDS = ("listen", "broker", "agent_grace", "lost_after")
"""The ``agents:`` settings the hub reads once, at start: changing one needs a restart."""


def _agents_on(agents: AgentsDef | None) -> bool:
    """Return whether ``agents`` turns agents on (it sets ``listen`` or ``broker``)."""
    return agents is not None and (agents.listen is not None or agents.broker is not None)


def _agents_moved(config: Config, running: Config, path: Path) -> list[Problem]:
    """Return a problem naming the ``agents:`` settings ``config`` changes that need a restart.

    Those are where agents meet the scheduler (``listen``, ``broker``) and
    the hub's timings (``agent_grace``, ``lost_after``). Turning agents on
    or off counts too: the hub is set up once, at start.
    """
    new, old = config.agents, running.agents
    on_new, on_old = _agents_on(new), _agents_on(old)
    if on_new != on_old:
        changed = ["agents (turning agents on or off)"]
    elif new is None or old is None or not on_new:
        return []
    else:
        changed = [f for f in _RESTART_FIELDS if getattr(new, f) != getattr(old, f)]
    if not changed:
        return []
    either = new or old
    file, line = (str(path), 1) if either is None else (either.source.file, either.source.line)
    message = f"agents: changing {', '.join(changed)} needs a restart"
    return [Problem(file, line, message)]


def config_actions(
    engine: Engine,
    path: Path,
    *,
    env: Mapping[str, str] | None = None,
    tokens: TokenHolder | None = None,
    users: UserHolder | None = None,
    on_failed: Callable[[Sequence[Problem]], None] | None = None,
    agent_secrets: Callable[[AgentSecrets], None] | None = None,
) -> tuple[Reload, Validate]:
    """Return ``(reload, validate)`` callables for the config at ``path``.

    Both load the files on a helper thread. ``validate`` returns every
    problem (errors and warnings). ``reload`` returns the errors and
    leaves the engine alone if there are any; otherwise it hands the new
    config to ``engine.reload`` (which may raise ``ActionError``) and
    returns the warnings.

    With ``tokens``, ``reload`` also re-reads the new config's
    ``api_tokens_file`` and swaps it into the holder once the config is
    in. A tokens file that cannot be read, or a change between
    development mode and tokens, fails the whole reload. ``users`` works
    the same way for the ``users_file``. Each swap tells the holder's
    watchers (the web UI then ends its open streams).

    The agents' secrets file is read with the config (a bad one fails the
    whole reload). Once the config is in, its secrets go to
    ``agent_secrets``, even when no other file changed. Moving the
    ``agents:`` ``listen`` or ``broker`` (or adding or removing the
    section) fails the reload: that needs a restart.

    A reload that comes back with errors (the files, the tokens or the users)
    calls ``on_failed`` with those error problems, whoever asked for it.
    """

    def _failed(problems: Sequence[Problem]) -> None:
        """Tell ``on_failed`` about a reload that found these error problems."""
        if on_failed is not None:
            on_failed([p for p in problems if p.severity == "error"])

    async def load() -> tuple[Config | None, list[Problem]]:
        """Load the config files, returning the config or its errors."""
        zone = engine.config.settings.timezone
        today = engine.clock.now().astimezone(zone).date()
        try:
            config = await run_in_thread(lambda: load_config(path, env=env, today=today))
        except ConfigError as exc:
            return None, list(exc.problems)
        return config, list(config.warnings)

    def reread(config: Config) -> tuple[TokenTable | None, UserTable | None, list[Problem]]:
        """Read the tokens and users files the new config names (on a helper thread)."""
        token_table, user_table, trouble = None, None, list[Problem]()
        if tokens is not None:
            token_table, found = _reread_tokens(config, path, tokens)
            trouble += found
        if users is not None:
            user_table, found = _reread_users(config, path, users)
            trouble += found
        return token_table, user_table, trouble

    async def reload(actor: str) -> list[Problem]:
        """Load the files (and the tokens and users) and, if they are valid, swap them in."""
        config, problems = await load()
        if config is None:
            _failed(problems)
            return problems
        loaded = config
        token_table, user_table, trouble = await run_in_thread(lambda: reread(loaded))
        trouble += _agents_moved(config, engine.config, path)
        if trouble:
            _failed(trouble)
            return trouble + problems
        await engine.reload(config, actor=actor)
        if tokens is not None:
            tokens.swap(token_table)
        if users is not None:
            users.swap(user_table)
        if agent_secrets is not None and config.agents is not None:
            agent_secrets(config.agents.secrets)
        return problems

    async def validate() -> list[Problem]:
        """Load the files and report what is wrong with them."""
        return (await load())[1]

    return reload, validate


def _answer(problems: list[Problem], engine: Engine) -> JsonObject:
    """Return ``{"ok": true, "warnings": [...]}``, or raise if any problem is an error.

    Messages hide the running config's expanded values too (the loader
    already hid the new config's).

    Raises:
        ApiError: 400 ``config has problems`` with every problem.

    """
    expansions = engine.config.expansions
    listed: list[JsonValue] = [problem_json(p, expansions) for p in problems]
    if any(p.severity == "error" for p in problems):
        raise ApiError(BAD_REQUEST, "config has problems", problems=listed)
    return {"ok": True, "warnings": listed}


def add_admin_routes(app: App, engine: Engine, *, reload: Reload, validate: Validate) -> None:
    """Register ``/api/health``, ``/api/reload`` and ``/api/config/validate`` on ``app``."""

    @app.get(HEALTH_PATH)
    async def health(request: HttpRequest) -> HttpResponse:
        """Say whether the engine is working, with its time zone and today's date there.

        No token is needed, so nothing more is given (and nothing at all
        once the engine has failed).
        """
        del request
        if engine.error is not None:
            return json({"status": "failed"}, UNAVAILABLE)
        zone = engine.config.settings.timezone
        today = engine.clock.now().astimezone(zone).date()
        return json({"status": "ok", "timezone": zone.key, "today": today.isoformat()})

    @app.post("/api/reload")
    @guarded
    async def reload_config(request: HttpRequest) -> JsonObject:
        """Reload the config files from disk."""
        return _answer(await reload(actor_of(request)), engine)

    @app.get("/api/config/validate")
    @guarded
    async def validate_config(request: HttpRequest) -> JsonObject:
        """Check the config files on disk without loading them."""
        del request
        return _answer(await validate(), engine)
