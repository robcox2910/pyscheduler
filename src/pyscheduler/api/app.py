"""Build the JSON HTTP API: one pyweb ``App`` in front of the engine.

Think of the API as the front desk of a train station. Travellers (the
CLI, scripts, a web page) come to the desk instead of walking onto
the tracks. The desk checks their pass (the access layer), answers
questions from the timetable (reads), and passes instructions to the
controller (actions) -- always in the same plain language (JSON).

The routes, exactly as the design lists them:

- ``GET /api/health`` (no token)
- ``GET /api/jobs``; ``GET /api/jobs/{name}``
- ``GET /api/runs?run_date=&job=&state=&limit=``; ``GET /api/runs/{id}``
- ``GET /api/runs/{id}/log?stream=stdout&attempt=&tail=200``
- ``GET /api/forecast?from=&to=``
- ``POST /api/jobs/{name}/trigger``; ``POST /api/jobs/{name}/backfill``
- ``POST /api/runs/{id}/{rerun|hold|release|kill|force-start|skip|mark-success|mark-failed}``
- ``GET /api/alerts?kind=&job=&since=&limit=``; ``GET /api/alerts/dead?limit=``
- ``POST /api/alerts/{id}/replay``
- ``POST /api/reload``; ``GET /api/config/validate``
"""

from pyweb.app import App

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.api.access import build_access
from pyscheduler.api.admin import Reload, Validate, add_admin_routes
from pyscheduler.api.alerts import add_alert_routes
from pyscheduler.api.auth import TokenHolder, TokenTable
from pyscheduler.api.jobs import add_job_routes
from pyscheduler.api.runs import add_run_routes
from pyscheduler.api.users import UserHolder
from pyscheduler.engine.core import Engine

__all__ = ["build_app"]


def build_app(
    engine: Engine,
    *,
    tokens: TokenTable | TokenHolder | None,
    reload: Reload,
    validate: Validate,
    dispatcher: Dispatcher | None = None,
    users: UserHolder | None = None,
    secure: bool = False,
) -> App:
    """Return the API app for ``engine``.

    Args:
        engine: The running engine the API reads from and acts on.
        tokens: The bearer tokens (a ``TokenHolder`` lets a reload swap
            them), or ``None`` for no tokens file.
        reload: Reload the config for an actor (see ``config_actions``).
        validate: Check the config files on disk (see ``config_actions``).
        dispatcher: The alert dispatcher (replays go through it); with ``None``
            the alert routes answer 503 ``alerts are not running``.
        users: The web UI's users (a ``UserHolder`` lets a reload swap
            them), or ``None`` for no users file. With neither tokens nor
            users the app is in development mode (everyone is ``dev``
            with the admin role; loopback hosts only).
        secure: Mark the session cookie ``Secure`` (off on loopback).

    Building it reads (or makes) ``state_dir/session.key`` unless in
    development mode: in a running service, build it on a helper thread.

    """
    app = App()
    token_holder = tokens if isinstance(tokens, TokenHolder) else TokenHolder(tokens)
    build_access(
        app,
        tokens=token_holder,
        users=users or UserHolder(None),
        state_dir=engine.config.settings.state_dir,
        secure=secure,
    )
    add_admin_routes(app, engine, reload=reload, validate=validate)
    add_job_routes(app, engine)
    add_run_routes(app, engine)
    add_alert_routes(app, engine, dispatcher)
    return app
