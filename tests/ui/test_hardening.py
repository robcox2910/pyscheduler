"""Test the security headers on error pages, the login ``next`` query, and expired sessions.

The page script reloads itself when the server says a session is gone
(ruling FR1); these tests pin the answers it relies on: a fragment, a
stream or an action from a stale cookie gets ``401``, and a page load
(``Accept: text/html``) is sent to the login page.
"""

from urllib.parse import quote

from api_helpers import DEV, EXTRACT_RUN, ApiHarness, MakeApi, log_in, users_yaml
from pyweb.messages import HttpRequest
from pyweb.testing import TestResponse
from ui_helpers import find, sign_in

from pyscheduler.api.access import SESSION_COOKIE
from pyscheduler.ui.layout import SECURITY_HEADERS

OK = 200
SEE_OTHER = 303
UNAUTHORISED = 401
FORBIDDEN = 403
NOT_FOUND = 404
SERVER_ERROR = 500
HTML = {"Accept": "text/html"}
FETCH = {"Accept": "*/*"}
"""What a browser's ``fetch`` sends by default (not ``text/html``)."""
STREAM = {"Accept": "text/event-stream"}
"""What a browser's ``EventSource`` sends."""
WANTED = "/timeline?zoom=week&date=2026-10-19"
BROKEN = "/broken"


def _site(api: MakeApi, *, live: bool = False) -> ApiHarness:
    """Return a harness with a users file (ada the admin), tokens too."""
    return api(users=users_yaml(ada="admin"), live=live)


def _secured(response: TestResponse) -> bool:
    """Return whether the response carries every security header."""
    return all(response.headers.get(name) == value for name, value in SECURITY_HEADERS.items())


class TestErrorPageHeaders:
    """pyweb's own HTML error pages get the security headers too."""

    def test_not_found_page(self, api: MakeApi) -> None:
        """An unknown UI path (logged in) is a 404 HTML page with the headers."""
        h_ = _site(api)

        async def body() -> None:
            """Ask for a page that does not exist."""
            log_in(h_, "ada")
            response = await h_.client.get("/no-such-page", headers=HTML)
            assert response.status == NOT_FOUND
            assert _secured(response)

        h_.run(body)

    def test_csrf_refusal_page(self, api: MakeApi) -> None:
        """A cookie form post without the CSRF token is a 403 HTML page with the headers."""
        h_ = _site(api)

        async def body() -> None:
            """Post the logout form without its token."""
            log_in(h_, "ada")
            response = await h_.client.post("/logout", form={}, headers=HTML)
            assert response.status == FORBIDDEN
            assert _secured(response)

        h_.run(body)

    def test_no_users_file_page(self, api: MakeApi) -> None:
        """Tokens only: a UI page is a 403 HTML page with the headers."""
        h_ = api()

        async def body() -> None:
            """Open the board without a users file."""
            response = await h_.client.get("/", headers=HTML)
            assert response.status == FORBIDDEN
            assert _secured(response)

        h_.run(body)

    def test_server_error_page(self, api: MakeApi) -> None:
        """A page that crashes is a 500 HTML page with the headers; JSON stays untouched."""
        h_ = api(tokens=False)

        @h_.client.app.get(BROKEN)
        async def broken(request: HttpRequest) -> None:
            """Fail."""
            del request
            raise RuntimeError

        async def body() -> None:
            """Open the broken page, as a browser and as a script."""
            page = await h_.client.get(BROKEN, headers={**DEV, **HTML})
            assert page.status == SERVER_ERROR
            assert _secured(page)
            plain = await h_.client.get("/api/jobs", headers=DEV)
            assert plain.headers.get("Content-Security-Policy") is None

        del broken
        h_.run(body)


class TestLoginKeepsQuery:
    """The login redirect keeps the page's query string."""

    def test_next_has_the_query(self, api: MakeApi) -> None:
        """A stranger opening a timeline week is sent to login and then back to that week."""
        h_ = _site(api)

        async def body() -> None:
            """Open the week logged out, follow to login, sign in."""
            away = await h_.client.get(WANTED, headers=HTML)
            assert away.status == SEE_OTHER
            location = away.headers.get("Location") or ""
            assert location == f"/login?next={quote(WANTED, safe='/')}"
            form = await h_.client.get(location, headers=HTML)
            (field,) = find(form.text, "input", name="next")
            assert field[1].get("value") == WANTED
            right = await sign_in(h_, "ada", next_path=WANTED)
            assert right.status == SEE_OTHER
            assert right.headers.get("Location") == WANTED

        h_.run(body)

    def test_next_without_query(self, api: MakeApi) -> None:
        """A path without a query is sent as before (no stray ``?``)."""
        h_ = _site(api)

        async def body() -> None:
            """Open the graph logged out."""
            away = await h_.client.get("/graph", headers=HTML)
            assert away.headers.get("Location") == "/login?next=/graph"

        h_.run(body)


class TestStaleSession:
    """What a page holding a stale cookie hears back (the script reloads on these)."""

    def test_stale_cookie_answers(self, api: MakeApi) -> None:
        """Fragments, streams and actions give 401; a page load goes to the login page."""
        h_ = _site(api, live=True)

        async def body() -> None:
            """Hold a cookie that no longer checks out, and ask for each thing."""
            h_.client.cookies[SESSION_COOKIE] = "expired.or.forged"
            row = await h_.client.get(f"/ui/fragments/board-row/{EXTRACT_RUN}", headers=FETCH)
            assert row.status == UNAUTHORISED
            counts = await h_.client.get("/ui/fragments/board-counts", headers=FETCH)
            assert counts.status == UNAUTHORISED
            dead = await h_.client.get("/ui/fragments/dead-letters", headers=FETCH)
            assert dead.status == UNAUTHORISED
            assert (await h_.client.get("/events", headers=STREAM)).status == UNAUTHORISED
            log = await h_.client.get(f"/events/log/{EXTRACT_RUN}", headers=STREAM)
            assert log.status == UNAUTHORISED
            probe = await h_.client.get(f"/api/runs/{EXTRACT_RUN}", headers=FETCH)
            assert probe.status == UNAUTHORISED
            action = await h_.client.post(
                f"/api/runs/{EXTRACT_RUN}/kill",
                json={},
                headers={**FETCH, "X-CSRF-Token": "stale"},
            )
            assert action.status == UNAUTHORISED
            assert action.json() == {"error": "unauthorised"}
            page = await h_.client.get("/", headers=HTML)
            assert page.status == SEE_OTHER
            assert page.headers.get("Location") == "/login?next=/"

        h_.run(body)

    def test_fresh_cookie_answers(self, api: MakeApi) -> None:
        """With a good cookie the same fragment and probe answer normally (not 401)."""
        h_ = _site(api)

        async def body() -> None:
            """Log in and ask for the counts and a missing run."""
            log_in(h_, "ada")
            counts = await h_.client.get("/ui/fragments/board-counts", headers=FETCH)
            assert counts.status == OK
            probe = await h_.client.get("/api/runs/nope.2026-10-19.1", headers=FETCH)
            assert probe.status == NOT_FOUND

        h_.run(body)
