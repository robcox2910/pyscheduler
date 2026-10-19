"""Login and logout: the front door of the web UI.

You give your name and password at the desk. If they match the guest
list you get a cloakroom ticket (the session cookie) and walk on to the
page you wanted. Too many wrong tries and the desk makes you wait.
"""

from pyweb.app import App
from pyweb.auth import SessionAuth, UserStore, authenticate, safe_next
from pyweb.html import html_template
from pyweb.messages import HttpRequest, HttpResponse, html, redirect
from pyweb.security import LoginThrottle

from pyscheduler.api.access import CSRF_STATE, LOGIN_PATH
from pyscheduler.ui.html import Safe, h
from pyscheduler.ui.layout import TEMPLATES, secure

__all__ = ["LOGIN_TEMPLATE", "WRONG", "add_login"]

LOGIN_TEMPLATE = (TEMPLATES / "login.html").read_text(encoding="utf-8")
"""The login page shell, read once at import."""

WRONG = "wrong name or password"
_UNAUTHORISED = 401


def _form(next_path: str, csrf: str) -> Safe:
    """Return the login form; ``csrf`` is set when a cookie user opens it again."""
    return h(
        "form",
        {"method": "post", "action": LOGIN_PATH},
        h("input", {"type": "hidden", "name": "next", "value": next_path}),
        h("input", {"type": "hidden", "name": "csrf_token", "value": csrf}) if csrf else None,
        h(
            "label",
            None,
            "Name",
            h(
                "input",
                {
                    "type": "text",
                    "name": "name",
                    "autocomplete": "username",
                    "required": True,
                    "autofocus": True,
                },
            ),
        ),
        h(
            "label",
            None,
            "Password",
            h(
                "input",
                {
                    "type": "password",
                    "name": "password",
                    "autocomplete": "current-password",
                    "required": True,
                },
            ),
        ),
        h("button", {"type": "submit", "class": "primary"}, "Sign in"),
    )


def _render(request: HttpRequest, next_path: str, *, error: bool) -> HttpResponse:
    """Return the login page (status 401 with the error message when ``error``)."""
    csrf = request.state.get(CSRF_STATE)
    message = h("p", {"class": "error", "role": "alert"}, WRONG) if error else None
    form = _form(safe_next(next_path), csrf if isinstance(csrf, str) else "")
    shell = html_template(LOGIN_TEMPLATE, {"message": message or "", "form": form})
    return secure(html(shell, _UNAUTHORISED if error else 200))


def add_login(app: App, session: SessionAuth, users: UserStore) -> None:
    """Add ``GET /login``, ``POST /login`` and ``POST /logout`` to ``app``."""
    throttle = LoginThrottle()

    @app.get(LOGIN_PATH)
    async def login_page(request: HttpRequest) -> HttpResponse:
        """Show the login form, remembering where the visitor was heading."""
        return _render(request, request.query.get("next") or "/", error=False)

    @app.post(LOGIN_PATH, blocking=True)
    def login(request: HttpRequest) -> HttpResponse:
        """Check the name and password; on success set the cookie and go on to ``next``."""
        form = request.form()
        next_path = form.get("next") or request.query.get("next") or "/"
        user = authenticate(users, throttle, form.get("name") or "", form.get("password") or "")
        if user is None:
            return _render(request, next_path, error=True)
        response = redirect(safe_next(next_path))
        session.login(response, user)
        return response

    @app.post("/logout")
    async def logout(request: HttpRequest) -> HttpResponse:
        """End the session (the access layer has checked the CSRF token) and go to login."""
        del request
        response = redirect(LOGIN_PATH)
        session.logout(response)
        return response

    del login_page, login, logout
