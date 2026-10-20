"""The page shell: top bar, who is logged in, and the security headers.

Every page of the web UI is a **picture frame** with a different
picture inside. The frame (this module and ``templates/page.html``)
holds the title bar, the menu, your name and the sign-out button; each
page only supplies the picture (``body``). The frame also stamps every
page with the security headers that tell the browser to run nothing but
our own files.
"""

import logging
from collections.abc import Mapping
from pathlib import Path

from pyweb.app import CallNext
from pyweb.html import html_template
from pyweb.messages import HTTPError, HttpRequest, HttpResponse, Outcome, html

from pyscheduler.api.access import CSRF_STATE
from pyscheduler.api.auth import Role, allows
from pyscheduler.api.support import actor_of, role_of
from pyscheduler.ui.html import Safe, h
from pyscheduler.ui.live import LIVE_STATE

INTERNAL_ERROR = 500
"""The status of the page for an error nobody handled."""

logger = logging.getLogger("pyscheduler.ui")

__all__ = [
    "NAV",
    "SECURITY_HEADERS",
    "TEMPLATES",
    "page",
    "secure",
    "secure_html",
    "sign_out_form",
]

TEMPLATES = Path(__file__).parent / "templates"
"""Where the page shells live (shipped inside the package)."""

PAGE_TEMPLATE = (TEMPLATES / "page.html").read_text(encoding="utf-8")
"""The page shell, read once at import (never from the event loop)."""

SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
"""Sent with every HTML page: only our own files may run, no framing, no sniffing."""

NAV: tuple[tuple[str, str, str, Role], ...] = (
    ("board", "/", "Board", "viewer"),
    ("graph", "/graph", "Graph", "viewer"),
    ("timeline", "/timeline", "Timeline", "viewer"),
    ("forecast", "/forecast", "Forecast", "viewer"),
    ("agents", "/agents", "Agents", "viewer"),
    ("admin", "/admin", "Admin", "admin"),
)
"""The menu: (key, link, label, the role that sees it)."""


def secure(response: HttpResponse) -> HttpResponse:
    """Add the security headers (and ``no-store``, as pages are private) and return it."""
    for name, value in SECURITY_HEADERS.items():
        response.headers.set(name, value)
    response.headers.set("Cache-Control", "no-store")
    return response


async def secure_html(request: HttpRequest, call_next: CallNext) -> Outcome:
    """Stamp the security headers on any HTML answer that lacks them (pyweb's error pages).

    Install it outermost, so the 404 for an unknown path, a refused CSRF
    token (raised as ``HTTPError`` by the session layer), the "needs a
    users_file" page and the generic 500 are covered as well. An ordinary
    exception from an inner middleware is logged and answered with the
    same generic 500 here (``BaseException``, such as ``Cancelled``, passes through).
    """
    try:
        outcome = await call_next(request)
    except HTTPError as exc:
        outcome = exc.to_response(request)
    except Exception:
        # An error raised inside an inner middleware would reach App.handle's
        # 500 outside this wrapper, without the headers: answer it here.
        logger.exception("Unhandled error in %s %s", request.method, request.path)
        outcome = HTTPError(INTERNAL_ERROR).to_response(request)
    if isinstance(outcome, HttpResponse):
        kind = (outcome.headers.get("Content-Type") or "").partition(";")[0].strip().lower()
        if kind == "text/html" and outcome.headers.get("Content-Security-Policy") is None:
            secure(outcome)
    return outcome


def sign_out_form(csrf: str) -> Safe:
    """Return the logout form, which posts the CSRF token as a form field."""
    return h(
        "form",
        {"method": "post", "action": "/logout"},
        h("input", {"type": "hidden", "name": "csrf_token", "value": csrf}),
        h("button", {"type": "submit", "class": "link"}, "Sign out"),
    )


def _nav(role: Role, active: str) -> Safe:
    """Return the menu links this role may see, marking the ``active`` one."""
    return h(
        "nav",
        None,
        *(
            h("a", {"href": href, "aria-current": "page" if key == active else None}, label)
            for key, href, label, needed in NAV
            if allows(role, needed)
        ),
    )


def page(request: HttpRequest, *, title: str, body: Safe, active: str) -> HttpResponse:
    """Return the full HTML page: the shell around ``body``, with the security headers.

    Args:
        request: The request (its caller is shown; a cookie user also
            gets a sign-out button and the CSRF token for the page's JS).
        title: The page title (escaped).
        body: The page's own HTML (built with ``ui.html``).
        active: The menu key of this page (``board``, ``graph``, ...).

    The caller must have passed the access layer (see ``role_of``). When
    the live feed is on, ``<body>`` carries ``data-live-after``: the newest
    event id when the request came in, where app.js resumes ``/events``.

    """
    role = role_of(request)
    csrf = request.state.get(CSRF_STATE)
    token = csrf if isinstance(csrf, str) else ""
    who = h(
        "div",
        {"class": "who"},
        h("span", None, f"{actor_of(request)} ({role})"),
        sign_out_form(token) if token else None,
    )
    after = request.state.get(LIVE_STATE)
    shell = html_template(
        PAGE_TEMPLATE,
        {
            "csrf": token,
            "live_after": after if isinstance(after, str) else "",
            "title": title,
            "nav": _nav(role, active),
            "who": who,
            "body": body,
        },
    )
    return secure(html(shell))
