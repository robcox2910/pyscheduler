"""Test the web UI shell: HTML helpers, login and logout, static files, headers."""

import html
import json
import subprocess
import sys
from datetime import timedelta
from urllib.parse import quote

import pytest
from alert_helpers import make_alert
from api_helpers import PHRASE, START, TODAY, ApiHarness, MakeApi, log_in, users_yaml
from ui_helpers import (
    HOSTILE,
    SHELL_PATH,
    add_shell,
    csrf_of,
    find,
    parse,
    seed,
    sign_in,
    stored_run,
)

from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.api.access import SESSION_COOKIE
from pyscheduler.engine.model import RunState, make_run_id
from pyscheduler.store import Batch
from pyscheduler.store.schema import ConfigRecord
from pyscheduler.ui.html import Safe, h, join

OK = 200
SEE_OTHER = 303
UNAUTHORISED = 401
NOT_FOUND = 404
FORBIDDEN = 403
TOO_MANY = 429
BAD_PASSWORDS = 5
MINUTE = timedelta(minutes=1)
NO_SLASH = HOSTILE.replace("/", "")
"""Hostile text a run id in a URL path can carry (a ``/`` would end the path segment)."""
NO_SLASH_RUN = make_run_id(NO_SLASH, TODAY, 1)
HOSTY_JOBS = f"""
jobs:
  hosty:
    command: run-hosty
    schedule: manual
    description: {json.dumps(HOSTILE)}
"""
HTML = {"Accept": "text/html"}
DEV_HTML = {"Accept": "text/html", "Host": "127.0.0.1:8470"}
PEOPLE = {"ada": "admin", "vic": "viewer", "ola": "operator"}
ESCAPED = "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;"
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
)


def _site(api: MakeApi, **people: str) -> ApiHarness:
    """Return a harness with users and the probe page."""
    h_ = api(tokens=False, users=users_yaml(**(people or PEOPLE)))
    add_shell(h_)
    return h_


class TestHtmlHelpers:
    """``h`` escapes everything and builds only allowed elements."""

    def test_h_escapes_text_and_attrs(self) -> None:
        """Text and attribute values are escaped; Safe children pass; flags and None work."""
        inner = h("b", None, HOSTILE)
        built = h(
            "div", {"title": HOSTILE, "hidden": None, "class": "x", "data-id": "1"}, "a<", inner
        )
        assert isinstance(built, Safe)
        assert "<script" not in built
        assert built == (
            f'<div title="{ESCAPED}" class="x" data-id="1">a&lt;<b>{ESCAPED}</b></div>'
        )
        assert h("input", {"disabled": True, "checked": False}) == "<input disabled>"
        assert join(h("br"), None, h("hr")) == "<br><hr>"
        with pytest.raises(TypeError, match="Safe"):
            join(HOSTILE)  # type: ignore[arg-type]
        for target in ("/\\evil.com", "\\\\evil.com", "/\\/evil.com", "//evil.com"):
            for attr, tag in (("action", "form"), ("href", "a")):
                with pytest.raises(ValueError, match="off this site"):
                    h(tag, {attr: target})
        for action in ("https://evil.com/x", "x"):
            with pytest.raises(ValueError, match="path on this site"):
                h("form", {"action": action})
        assert h("form", {"action": "/logout"}) == '<form action="/logout"></form>'

    def test_h_rejects_unknown_tag(self) -> None:
        """A tag, attribute or link target off the lists raises ``ValueError``."""
        with pytest.raises(ValueError, match="not allowed"):
            h("script", None, "alert(1)")
        with pytest.raises(ValueError, match="not allowed"):
            h("div", {"onclick": "x()"})
        with pytest.raises(ValueError, match="address"):
            h("a", {"href": " JaVa\tscript:alert(1)"}, "x")
        with pytest.raises(ValueError, match="no children"):
            h("br", None, "x")
        assert h("a", {"href": "/jobs/x?y=1#z"}, "ok") == '<a href="/jobs/x?y=1#z">ok</a>'


class TestLogin:
    """The login desk: forms, cookies, throttle and redirects."""

    def test_login_flow(self, api: MakeApi) -> None:
        """Logged out gives a redirect; wrong password 401; right 303 with cookie; logout clears."""
        h_ = _site(api)

        async def body() -> None:
            """Walk through login and logout."""
            away = await h_.client.get(SHELL_PATH, headers=HTML)
            assert away.status == SEE_OTHER
            assert away.headers.get("Location") == "/login?next=/shell"
            wrong = await sign_in(h_, "ada", "nope")
            assert wrong.status == UNAUTHORISED
            assert "wrong name or password" in wrong.text
            assert SESSION_COOKIE not in h_.client.cookies
            right = await sign_in(h_, "ada", next_path="/shell")
            assert right.status == SEE_OTHER
            assert right.headers.get("Location") == "/shell"
            assert SESSION_COOKIE in h_.client.cookies
            shell = await h_.client.get(SHELL_PATH)
            assert shell.status == OK
            out = await h_.client.post("/logout", form={"csrf_token": csrf_of(shell.text)})
            assert out.status == SEE_OTHER
            assert out.headers.get("Location") == "/login"
            assert SESSION_COOKIE not in h_.client.cookies
            assert (await h_.client.get(SHELL_PATH, headers=HTML)).status == SEE_OTHER

        h_.run(body)

    def test_login_throttle(self, api: MakeApi) -> None:
        """After five wrong passwords in a row the sixth try is 429, even with the right one."""
        h_ = _site(api)

        async def body() -> None:
            """Fail five times, then try again."""
            for _ in range(BAD_PASSWORDS):
                assert (await sign_in(h_, "ada", "nope")).status == UNAUTHORISED
            assert (await sign_in(h_, "ada", "nope")).status == TOO_MANY
            assert (await sign_in(h_, "ada", PHRASE)).status == TOO_MANY

        h_.run(body)

    def test_login_next_is_made_safe(self, api: MakeApi) -> None:
        """A ``next`` that leaves the site (``//evil.com``, ``https://...``) goes to ``/``."""
        h_ = _site(api)

        async def body() -> None:
            """Try hostile redirect targets in the form and in the query."""
            for target in ("//evil.com", "https://evil.com/x", "/\\evil.com", "evil"):
                h_.client.cookies.clear()
                done = await sign_in(h_, "ada", next_path=target)
                assert done.status == SEE_OTHER
                assert done.headers.get("Location") == "/"
            h_.client.cookies.clear()
            via_query = await h_.client.post(
                "/login?next=//evil.com", form={"name": "ada", "password": PHRASE}
            )
            assert via_query.headers.get("Location") == "/"
            form = await h_.client.get("/login?next=//evil.com")
            (hidden,) = find(form.text, "input", name="next")
            assert hidden[1]["value"] == "/"

        h_.run(body)

    def test_login_page_is_only_for_the_users_file(self, api: MakeApi) -> None:
        """Without a users file (dev mode) there is no login page."""
        h_ = api(tokens=False)

        async def body() -> None:
            """Ask for the login page in development mode."""
            assert (await h_.client.get("/login", headers=DEV_HTML)).status == NOT_FOUND

        h_.run(body)


class TestShell:
    """The page frame: headers, csrf, names and the menu."""

    def test_security_headers(self, api: MakeApi) -> None:
        """The login page and the shell both carry the CSP, nosniff and referrer headers."""
        h_ = _site(api)

        async def body() -> None:
            """Fetch a stranger's login page and a signed-in page."""
            pages = [await h_.client.get("/login")]
            await sign_in(h_, "ada")
            pages.append(await h_.client.get(SHELL_PATH))
            for response in pages:
                assert response.headers.get("Content-Security-Policy") == CSP
                assert response.headers.get("X-Content-Type-Options") == "nosniff"
                assert response.headers.get("Referrer-Policy") == "same-origin"
                assert response.headers.get("Cache-Control") == "no-store"
                assert "<script>" not in response.text
                assert not any(
                    key.startswith("on") for _, attrs, _ in parse(response.text) for key in attrs
                )

        h_.run(body)

    def test_static_served(self, api: MakeApi) -> None:
        """The CSS and JS are public; a path out of the folder is 404."""
        h_ = _site(api)

        async def body() -> None:
            """Fetch the static files with no login."""
            css = await h_.client.get("/static/app.css")
            assert css.status == OK
            assert (css.headers.get("Content-Type") or "").startswith("text/css")
            assert "--bg" in css.text
            assert (await h_.client.get("/static/app.js")).status == OK
            assert (await h_.client.get("/static/../config")).status == NOT_FOUND

        h_.run(body)

    def test_csrf_token_reaches_the_page(self, api: MakeApi) -> None:
        """A cookie user's page holds their CSRF token in the meta tag, and a sign-out form."""
        h_ = _site(api)
        csrf = log_in(h_, "ada")

        async def body() -> None:
            """Read the meta tag and the logout form."""
            text = (await h_.client.get(SHELL_PATH)).text
            assert csrf_of(text) == csrf
            (field,) = find(text, "input", name="csrf_token")
            assert field[1]["value"] == csrf
            assert "ada (admin)" in text

        h_.run(body)

    def test_dev_mode_shell(self, api: MakeApi) -> None:
        """Dev mode shows dev (admin), no sign-out button and an empty csrf token."""
        h_ = api(tokens=False)
        add_shell(h_)

        async def body() -> None:
            """Fetch the shell with a loopback Host."""
            text = (await h_.client.get(SHELL_PATH, headers=DEV_HTML)).text
            assert "dev (admin)" in text
            assert csrf_of(text) == ""
            assert not find(text, "form")
            assert [e[2] for e in find(text, "a")].count("Admin") == 1

        h_.run(body)

    def test_viewer_sees_no_admin_nav(self, api: MakeApi) -> None:
        """Only an admin gets the Admin link."""
        h_ = _site(api)

        async def body() -> None:
            """Compare a viewer's menu with an admin's."""
            seen: dict[str, list[str]] = {}
            for name in ("vic", "ada"):
                h_.client.cookies.clear()
                await sign_in(h_, name)
                text = (await h_.client.get(SHELL_PATH)).text
                (nav,) = find(text, "nav")
                seen[name] = [e[2] for e in find(text, "a") if e[2] in nav[2]]
            assert "Admin" not in seen["vic"]
            assert "Board" in seen["vic"]
            assert "Admin" in seen["ada"]

        h_.run(body)

    @pytest.mark.parametrize(
        ("path", "shows"),
        [
            (SHELL_PATH, HOSTILE),
            ("/login?next=/%22%3E%3Cscript%3Ex", None),
            ("/", HOSTILE),
            (f"/?q={quote(HOSTILE)}&tag={quote(HOSTILE)}", HOSTILE),
            (f"/?criticality={quote(HOSTILE)}", HOSTILE),
            (f"/ui/fragments/board-row/{quote(NO_SLASH_RUN, safe='')}", NO_SLASH),
            (f"/ui/fragments/board-row/{quote(NO_SLASH_RUN, safe='')}x", NO_SLASH),
            (f"/ui/fragments/board-counts?tag={quote(HOSTILE)}", None),
            ("/jobs/hosty", HOSTILE),
            (f"/runs/{quote(NO_SLASH_RUN, safe='')}", NO_SLASH),
            ("/graph", HOSTILE),
            (f"/graph?tag={quote(HOSTILE)}&focus={quote(HOSTILE)}", HOSTILE),
            (f"/graph?focus={quote(HOSTILE)}&run_date={quote(HOSTILE)}", HOSTILE),
            ("/timeline", HOSTILE),
            ("/timeline?zoom=week", HOSTILE),
            (f"/timeline?zoom={quote(HOSTILE)}&date={quote(HOSTILE)}", None),
            ("/forecast", None),
            (f"/forecast?from={quote(HOSTILE)}&to={quote(HOSTILE)}", None),
            ("/admin", HOSTILE),
            ("/admin/configs/1", HOSTILE),
            ("/ui/fragments/dead-letters", HOSTILE),
            ("/agents", None),
            (f"/ui/fragments/agent-row/{quote(NO_SLASH, safe='')}", NO_SLASH),
        ],
    )
    def test_every_page_escapes(self, api: MakeApi, path: str, shows: str | None) -> None:
        """Hostile names, reasons, titles and query text reach each page escaped, never raw.

        Later tasks add their pages to the parameter list. A run of a job
        named ``HOSTILE`` and one named ``NO_SLASH`` (each with its name as
        the reason too) are in the store for today; the page must contain
        ``shows`` escaped.
        """
        h_ = api(HOSTY_JOBS, tokens=False, users=users_yaml(ada="admin"))
        add_shell(h_, name=HOSTILE, body=HOSTILE)
        alert = make_alert()
        dead = DeliveryRecord(alert.alert_id, "hook", DeliveryStatus.DEAD, 1, HOSTILE, START)
        sources = json.dumps({"/x/jobs.yaml": f"{HOSTILE}\n"})
        h_.store.apply(
            Batch(alerts=[alert], deliveries=[dead], configs=[ConfigRecord(1, START, "d", sources)])
        )
        seed(
            h_,
            *(
                stored_run(name, RunState.FAILED, TODAY, START, reason=name, took=MINUTE)
                for name in (HOSTILE, NO_SLASH)
            ),
        )

        async def body() -> None:
            """Fetch the page and look for the raw text."""
            await sign_in(h_, "ada")
            text = (await h_.client.get(path)).text
            assert "<script>" not in text.replace('src="/static/app.js"', "")
            assert "<script" not in text.partition("<main")[2 if "<main" in text else 0]
            if shows is not None:
                assert html.escape(shows, quote=True) in text

        h_.run(body)


class TestSignedInLogin:
    """A signed-in user who opens the login form again can still submit it."""

    def test_login_form_carries_the_csrf_token(self, api: MakeApi) -> None:
        """The form holds the session's CSRF token, so a second sign-in is not refused."""
        h_ = _site(api)
        csrf = log_in(h_, "ada")

        async def body() -> None:
            """Open the form, then post it as another user."""
            form = await h_.client.get("/login")
            (field,) = find(form.text, "input", name="csrf_token")
            assert field[1]["value"] == csrf
            again = await h_.client.post(
                "/login", form={"name": "vic", "password": PHRASE, "csrf_token": csrf}
            )
            assert again.status == SEE_OTHER

        h_.run(body)


class TestLogout:
    """Logout needs the session's CSRF token; a stranger's logout is harmless."""

    def test_logout_needs_the_csrf_token(self, api: MakeApi) -> None:
        """A cookie user without a good token gets 403 and stays signed in."""
        h_ = _site(api)
        csrf = log_in(h_, "ada")

        async def body() -> None:
            """Try no token and a wrong token, then check the session still works."""
            assert (await h_.client.post("/logout", form={})).status == FORBIDDEN
            bad = await h_.client.post("/logout", form={"csrf_token": "x" + csrf})
            assert bad.status == FORBIDDEN
            assert (await h_.client.get(SHELL_PATH)).status == OK

        h_.run(body)

    def test_stranger_logout_is_harmless(self, api: MakeApi) -> None:
        """A logged-out POST /logout just goes to the login page."""
        h_ = _site(api)

        async def body() -> None:
            """Post with no session."""
            done = await h_.client.post("/logout", form={}, headers=HTML)
            assert done.status == SEE_OTHER
            assert (done.headers.get("Location") or "").startswith("/login")
            assert done.headers.get("Set-Cookie") is None
            assert SESSION_COOKIE not in h_.client.cookies

        h_.run(body)

    def test_ui_imports_on_its_own(self) -> None:
        """The ui and api packages import in a fresh interpreter, in either order."""
        for code in (
            "import pyscheduler.ui.html; import pyscheduler.ui",
            "import pyscheduler.ui.layout",
            "import pyscheduler.ui.login",
            "import pyscheduler.ui.live",
            "import pyscheduler.ui.logtail",
            "import pyscheduler.ui.board",
            "import pyscheduler.ui.timeline",
            "import pyscheduler.ui.forecast_page",
            "import pyscheduler.api; import pyscheduler.ui",
        ):
            run = subprocess.run(  # noqa: S603 - fixed arguments
                [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
            )
            assert run.returncode == 0, run.stderr
