"""One access layer for API tokens, browser sessions and development mode.

The scheduler has one front door with one doorman, whoever is knocking.
A script shows a **wristband** (an API token in ``Authorization:
Bearer``); a browser shows a **cloakroom ticket** (a signed session
cookie from the login page). Either way the doorman writes the same
badge, ``Principal(name, role)``, and every room checks only the badge.

pyweb's ``SessionAuth`` reads the ticket or the wristband (our token
table is its ``bearer_verifier``) and checks the CSRF token on a
cookie's ``POST``. Inside it, the access middleware turns pyweb's
``User`` into a ``Principal`` and applies the API's rules:

- ``/api/health`` and ``/static/...`` need nobody;
- ``/api/...`` needs a caller (401 ``{"error": "unauthorised"}``), a
  viewer may only read (403 ``{"error": "forbidden"}``), and a ``POST``
  must say it is JSON (415);
- every other path is the web UI: it needs a users file (403 "the web UI
  needs a users_file" otherwise) and a logged-in caller (401, or a 303
  to ``/login?next=...`` for a browser), except ``/login`` itself.

With no tokens file and no users file there is no doorman, only a
**development mode** guard: the ``Host`` must be this machine (every
path), everyone is ``dev`` with the admin role, and every ``POST`` must
be JSON (the cross-site guard when there is no login).
"""

from collections.abc import Callable
from urllib.parse import quote

from pyweb.app import App, CallNext, Middleware
from pyweb.auth import SessionAuth, User
from pyweb.messages import HTTPError, HttpRequest, Outcome, json, redirect
from pyweb.security import SessionSigner

from pyscheduler.api.auth import (
    DEV_PRINCIPAL,
    HEALTH_PATH,
    READ_METHODS,
    ROLE_RANK,
    Principal,
    TokenHolder,
    allows,
    is_loopback_host,
)
from pyscheduler.api.users import UserHolder

__all__ = [
    "API_PREFIX",
    "CSRF_STATE",
    "LOGIN_PATH",
    "NEEDS_USERS",
    "SESSION_COOKIE",
    "STATIC_PREFIX",
    "build_access",
    "needs_session_key",
    "principal_of",
]

SESSION_COOKIE = "pysched_session"
"""The name of the browser's session cookie."""

LOGIN_PATH = "/login"
"""The login page: the one UI path a stranger may open."""

API_PREFIX = "/api/"
STATIC_PREFIX = "/static/"
"""Static files (CSS, JS) are public: they hold no data."""

CSRF_STATE = "pyscheduler.csrf"
"""``request.state`` key holding a cookie caller's CSRF token, for pages to embed."""

NEEDS_USERS = "the web UI needs a users_file"

_FORBIDDEN = 403
_UNAUTHORISED = 401
_UNSUPPORTED_MEDIA = 415
_JSON_TYPE = "application/json"


def principal_of(user: object) -> Principal | None:
    """Return the ``Principal`` for a pyweb ``User`` (its highest known role), or ``None``."""
    if not isinstance(user, User):
        return None
    known = frozenset(ROLE_RANK).intersection(user.roles)
    return Principal(user.name, max(known, key=ROLE_RANK.__getitem__)) if known else None


def _verifier(tokens: TokenHolder) -> Callable[[str], User | None]:
    """Return the bearer verifier: a token-file entry becomes a passwordless ``User``.

    It is an in-memory hash lookup, cheap enough for the event loop.
    """

    def verify(token: str) -> User | None:
        """Return the user the token names in the current table, or ``None``."""
        table = tokens.table
        found = None if table is None else table.lookup(token)
        if found is None:
            return None
        name, role = found
        return User(name, "", frozenset({role}))

    return verify


def _is_json(request: HttpRequest) -> bool:
    """Return whether the request says its body is JSON (a charset may follow)."""
    content_type = request.headers.get("Content-Type") or ""
    return content_type.partition(";")[0].strip().lower() == _JSON_TYPE


def _sends_bearer(request: HttpRequest) -> bool:
    """Return whether the request carries ``Authorization: Bearer`` (so no cookie is used)."""
    header = request.headers.get("Authorization")
    return header is not None and header.strip().partition(" ")[0].lower() == "bearer"


def _unauthorised() -> Outcome:
    """Build the 401 answer that asks for a bearer token."""
    response = json({"error": "unauthorised"}, _UNAUTHORISED)
    response.headers.set("WWW-Authenticate", "Bearer")
    return response


async def _typed(request: HttpRequest, call_next: CallNext) -> Outcome:
    """Refuse a ``POST`` whose body is not declared JSON (415); otherwise go on."""
    if request.method == "POST" and not _is_json(request):
        return json({"error": "use Content-Type: application/json"}, _UNSUPPORTED_MEDIA)
    return await call_next(request)


def _dev_middleware() -> Middleware:
    """Return development mode's guard: loopback ``Host`` only, everyone ``dev``, JSON POSTs."""

    async def check(request: HttpRequest, call_next: CallNext) -> Outcome:
        """Check the host, make the caller ``dev`` and check the body type."""
        if not is_loopback_host(request.headers.get("Host")):
            return json({"error": "forbidden host"}, _FORBIDDEN)
        if request.path == HEALTH_PATH:
            return await call_next(request)
        request.user = DEV_PRINCIPAL
        return await _typed(request, call_next)

    return check


def _ui_refused(request: HttpRequest) -> Outcome:
    """Answer a UI request when there is no users file: 403, as HTML for a browser."""
    if request.wants_html:
        return HTTPError(_FORBIDDEN, NEEDS_USERS).to_response(request)
    return json({"error": NEEDS_USERS}, _FORBIDDEN)


async def _api_rules(
    request: HttpRequest, principal: Principal | None, call_next: CallNext
) -> Outcome:
    """Apply the API's rules: a caller (401), a viewer only reads (403), JSON POSTs (415)."""
    if principal is None:
        return _unauthorised()
    if not allows(principal.role, "operator") and request.method not in READ_METHODS:
        return json({"error": "forbidden"}, _FORBIDDEN)
    return await _typed(request, call_next)


async def _stranger(request: HttpRequest, call_next: CallNext) -> Outcome:
    """Let a stranger see the login page; send a browser there from any other page (else 401)."""
    if request.path == LOGIN_PATH:
        return await call_next(request)
    if request.wants_html:
        return redirect(f"{LOGIN_PATH}?next={quote(request.path)}")
    return _unauthorised()


def _access_middleware(session: SessionAuth, *, web_ui: bool) -> Middleware:
    """Return the middleware (inside ``session``) that sets the ``Principal`` and the rules."""

    async def check(request: HttpRequest, call_next: CallNext) -> Outcome:
        """Turn the user into a ``Principal``, then apply the API or the UI rules."""
        principal = principal_of(request.user)
        request.user = principal
        path = request.path
        if path == HEALTH_PATH or path.startswith(STATIC_PREFIX):
            return await call_next(request)
        if path.startswith(API_PREFIX):
            return await _api_rules(request, principal, call_next)
        if not web_ui:
            return _ui_refused(request)
        if principal is None:
            return await _stranger(request, call_next)
        if not _sends_bearer(request):
            csrf = session.csrf_token(request)
            if csrf is not None:
                request.state[CSRF_STATE] = csrf
        return await call_next(request)

    return check


def needs_session_key(tokens: TokenHolder, users: UserHolder) -> bool:
    """Return whether ``build_access`` needs a session key (everything but development mode)."""
    return tokens.table is not None or users.table is not None


def build_access(
    app: App, *, tokens: TokenHolder, users: UserHolder, key: str | None, secure: bool
) -> SessionAuth | None:
    """Install the access layer on ``app``; return the ``SessionAuth`` the login pages use.

    Args:
        app: The app to guard (call this before adding other middleware).
        tokens: The API tokens (``table`` ``None``: no tokens file).
        users: The web UI's users (``table`` ``None``: no users file).
        key: The session key (see ``load_or_make_key``, which reads a file:
            load it on a helper thread and pass it in). Development mode
            has no sessions and ignores it.
        secure: Mark the session cookie ``Secure`` (not on a loopback address).

    With neither tokens nor users this is development mode and returns
    ``None``. Otherwise it installs ``SessionAuth`` and the access
    middleware; it returns the ``SessionAuth`` when there is a users file
    (so there is a web UI to log in to), else ``None``.

    Raises:
        ValueError: If a key is needed (see ``needs_session_key``) but is ``None``.

    """
    if not needs_session_key(tokens, users):
        app.middleware(_dev_middleware())
        return None
    if key is None:
        msg = "a session key is needed unless in development mode"
        raise ValueError(msg)
    session = SessionAuth(
        SessionSigner(key),
        users,
        cookie_name=SESSION_COOKIE,
        login_path=LOGIN_PATH,
        secure_cookies=secure,
        bearer_verifier=_verifier(tokens),
    )
    web_ui = users.table is not None
    app.middleware(session)
    app.middleware(_access_middleware(session, web_ui=web_ui))
    return session if web_ui else None
