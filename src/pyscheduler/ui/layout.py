"""The page shell: top bar, who is logged in, and the security headers.

Every page of the web UI is a **picture frame** with a different
picture inside. The frame (this module and ``templates/page.html``)
holds the title bar, the menu, your name and the sign-out button; each
page only supplies the picture (``body``). The frame also stamps every
page with the security headers that tell the browser to run nothing but
our own files.
"""

from collections.abc import Mapping
from pathlib import Path

from pyweb.html import html_template
from pyweb.messages import HttpRequest, HttpResponse, html

from pyscheduler.api.access import CSRF_STATE
from pyscheduler.api.auth import Role, allows
from pyscheduler.api.support import actor_of, role_of
from pyscheduler.ui.html import Safe, h

__all__ = ["NAV", "SECURITY_HEADERS", "TEMPLATES", "page", "secure", "sign_out_form"]

TEMPLATES = Path(__file__).parent / "templates"
"""Where the page shells live (shipped inside the package)."""

PAGE_TEMPLATE = (TEMPLATES / "page.html").read_text(encoding="utf-8")
"""The page shell, read once at import (never from the event loop)."""

SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'"
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
    ("admin", "/admin", "Admin", "admin"),
)
"""The menu: (key, link, label, the role that sees it)."""


def secure(response: HttpResponse) -> HttpResponse:
    """Add the security headers to ``response`` and return it."""
    for name, value in SECURITY_HEADERS.items():
        response.headers.set(name, value)
    return response


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

    The caller must have passed the access layer (see ``role_of``).

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
    shell = html_template(
        PAGE_TEMPLATE,
        {"csrf": token, "title": title, "nav": _nav(role, active), "who": who, "body": body},
    )
    return secure(html(shell))
