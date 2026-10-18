"""The web UI: server-rendered pages, static files and the login desk."""

from pathlib import Path

from pyweb.app import App
from pyweb.auth import SessionAuth, UserStore

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.api.access import STATIC_PREFIX
from pyscheduler.engine.core import Engine
from pyscheduler.ui.login import add_login

__all__ = ["STATIC_DIR", "add_ui"]

STATIC_DIR = Path(__file__).parent / "static"
"""The CSS and JS the pages load (shipped inside the package)."""

STATIC_MAX_AGE = 300


def add_ui(
    app: App,
    engine: Engine,
    *,
    session_auth: SessionAuth | None,
    users: UserStore,
    dispatcher: Dispatcher | None,
) -> None:
    """Add the web UI to ``app``: static files, and login when there is a users file.

    Args:
        app: The app (the access layer is already installed).
        engine: The engine the pages read (used by the pages later tasks add).
        session_auth: The ``SessionAuth`` from ``build_access``; ``None``
            when there is no login (development mode, or no users file).
        users: The user table the login page checks.
        dispatcher: The alert dispatcher (for the admin pages).

    """
    del engine, dispatcher  # the pages that use them are added by later tasks
    app.mount_static(STATIC_PREFIX.rstrip("/"), STATIC_DIR, max_age=STATIC_MAX_AGE)
    if session_auth is not None:
        add_login(app, session_auth, users)
