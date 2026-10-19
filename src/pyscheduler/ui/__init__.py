"""The web UI: server-rendered pages, static files, the login desk and the live feeds."""

from pathlib import Path

from pyweb.app import App
from pyweb.auth import SessionAuth, UserStore

from pyscheduler.alerts.dispatch import Dispatcher
from pyscheduler.api.access import STATIC_PREFIX
from pyscheduler.engine.core import Engine
from pyscheduler.ui.board import add_board
from pyscheduler.ui.detail import add_detail
from pyscheduler.ui.graph import add_graph
from pyscheduler.ui.live import LiveFeed, add_live
from pyscheduler.ui.login import add_login
from pyscheduler.ui.logtail import add_log_tail

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
    live: LiveFeed | None = None,
) -> None:
    """Add the web UI to ``app``: static files, login (with a users file), the pages and live feeds.

    Args:
        app: The app (the access layer is already installed).
        engine: The engine the pages read.
        session_auth: The ``SessionAuth`` from ``build_access``; ``None``
            when there is no login (development mode, or no users file).
        users: The user table the login page checks.
        dispatcher: The alert dispatcher (for the admin pages).
        live: The live feed attached to ``engine``, served at ``/events``;
            with ``None`` there is no ``/events``. The log tail
            (``/events/log/<run_id>``) is always there.

    """
    del dispatcher  # the pages that use it are added by later tasks
    app.mount_static(STATIC_PREFIX.rstrip("/"), STATIC_DIR, max_age=STATIC_MAX_AGE)
    if session_auth is not None:
        add_login(app, session_auth, users)
    if live is not None:
        add_live(app, live)
    add_log_tail(app, engine)
    add_board(app, engine)
    add_detail(app, engine)
    add_graph(app, engine)
