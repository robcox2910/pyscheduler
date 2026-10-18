"""The admin endpoints: health, reload and validate.

``reload`` and ``validate`` are handed in as callables, so the API does
not need to know where the config files live. ``config_actions`` makes
the usual pair: both read the files on disk on a helper thread;
``reload`` then swaps the new config into the engine (and re-reads the
tokens file, so a revoked token stops working), ``validate`` only
reports.
"""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse, json

from pyscheduler.api.auth import HEALTH_PATH, TokenFileError, TokenHolder, TokenTable
from pyscheduler.api.serialize import problem_json
from pyscheduler.api.support import BAD_REQUEST, ApiError, actor_of, guarded
from pyscheduler.config import Config, ConfigError, Problem, load_config
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


def config_actions(
    engine: Engine,
    path: Path,
    *,
    env: Mapping[str, str] | None = None,
    tokens: TokenHolder | None = None,
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
    development mode and tokens, fails the whole reload.
    """

    async def load() -> tuple[Config | None, list[Problem]]:
        """Load the config files, returning the config or its errors."""
        zone = engine.config.settings.timezone
        today = engine.clock.now().astimezone(zone).date()
        try:
            config = await run_in_thread(lambda: load_config(path, env=env, today=today))
        except ConfigError as exc:
            return None, list(exc.problems)
        return config, list(config.warnings)

    async def reload(actor: str) -> list[Problem]:
        """Load the files (and the tokens) and, if they are valid, swap them in."""
        config, problems = await load()
        if config is None:
            return problems
        table = None
        if tokens is not None:
            loaded, holder = config, tokens
            table, trouble = await run_in_thread(lambda: _reread_tokens(loaded, path, holder))
            if trouble:
                return trouble + problems
        await engine.reload(config, actor=actor)
        if tokens is not None:
            tokens.table = table
        return problems

    async def validate() -> list[Problem]:
        """Load the files and report what is wrong with them."""
        return (await load())[1]

    return reload, validate


def _answer(problems: list[Problem]) -> JsonObject:
    """Return ``{"ok": true, "warnings": [...]}``, or raise if any problem is an error.

    Raises:
        ApiError: 400 ``config has problems`` with every problem.

    """
    if any(p.severity == "error" for p in problems):
        listed: list[JsonValue] = [problem_json(p) for p in problems]
        raise ApiError(BAD_REQUEST, "config has problems", problems=listed)
    return {"ok": True, "warnings": [problem_json(p) for p in problems]}


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
        return _answer(await reload(actor_of(request)))

    @app.get("/api/config/validate")
    @guarded
    async def validate_config(request: HttpRequest) -> JsonObject:
        """Check the config files on disk without loading them."""
        del request
        return _answer(await validate())
