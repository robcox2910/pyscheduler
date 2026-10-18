"""Build the JSON HTTP API: one pyweb ``App`` in front of the engine.

Think of the API as the front desk of a train station. Travellers (the
CLI, scripts, later a web page) come to the desk instead of walking onto
the tracks. The desk checks their pass (the token middleware), answers
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
- ``POST /api/reload``; ``GET /api/config/validate``
"""

from pyweb.app import App

from pyscheduler.api.admin import Reload, Validate, add_admin_routes
from pyscheduler.api.auth import TokenTable, token_middleware
from pyscheduler.api.jobs import add_job_routes
from pyscheduler.api.runs import add_run_routes
from pyscheduler.engine.core import Engine

__all__ = ["build_app"]


def build_app(
    engine: Engine, *, tokens: TokenTable | None, reload: Reload, validate: Validate
) -> App:
    """Return the API app for ``engine``.

    Args:
        engine: The running engine the API reads from and acts on.
        tokens: The bearer tokens, or ``None`` for development mode
            (everyone is ``dev`` with the operator role; only safe on
            loopback).
        reload: Reload the config for an actor (see ``config_actions``).
        validate: Check the config files on disk (see ``config_actions``).

    """
    app = App()
    app.middleware(token_middleware(tokens))
    add_admin_routes(app, engine, reload=reload, validate=validate)
    add_job_routes(app, engine)
    add_run_routes(app, engine)
    return app
