"""Test the one access layer: API tokens, web sessions and development mode."""

import stat
from pathlib import Path

import pynet.loop
import pytest
from api_helpers import (
    DEV,
    FORBIDDEN,
    OK,
    OPERATOR,
    TODAY,
    UNAUTHORISED,
    VIEWER,
    ApiHarness,
    MakeApi,
    body_of,
    log_in,
    users_yaml,
)
from pynet.messaging import JsonValue
from pyweb.app import App, CallNext
from pyweb.auth import User
from pyweb.messages import HttpRequest, Outcome
from pyweb.testing import TestClient

from pyscheduler.api import TokenHolder, UserHolder, UserTable, build_app, config_actions
from pyscheduler.api.access import build_access, needs_session_key, principal_of
from pyscheduler.api.session_key import KEY_FILE, SessionKeyError, load_or_make_key
from pyscheduler.api.support import actor_of, guarded, role_of
from pyscheduler.engine.model import RunState

SEE_OTHER = 303
UNSUPPORTED_MEDIA = 415
PRIVATE = 0o600
MIN_SECRET = 32
UI_NEEDS_USERS = {"error": "the web UI needs a users_file"}
HTML = {"Accept": "text/html"}
JSON_TYPE = {"Content-Type": "application/json"}
WHOAMI = "/api/whoami"
PEOPLE = {"ada": "admin", "ola": "operator", "vic": "viewer"}


def _whoami(h: ApiHarness) -> None:
    """Add ``GET /api/whoami``, answering the caller's ``actor_of`` and ``role_of``."""

    @h.client.app.get(WHOAMI)
    @guarded
    async def whoami(request: HttpRequest) -> dict[str, JsonValue]:
        """Say who the access layer says the caller is."""
        return {"name": actor_of(request), "role": role_of(request)}

    del whoami


class TestTokens:
    """Bearer tokens work as in phase 1, with or without a users file."""

    def test_bearer_tokens_still_work(self, api: MakeApi) -> None:
        """Viewer tokens read, operator tokens act, a viewer POST is 403, no token 401."""
        h = api(users=users_yaml(**PEOPLE))
        _whoami(h)

        async def body() -> None:
            """Try each kind of caller."""
            missing = await h.client.get("/api/jobs")
            assert missing.status == UNAUTHORISED
            assert body_of(missing) == {"error": "unauthorised"}
            assert missing.headers.get("WWW-Authenticate") == "Bearer"
            assert (await h.client.get("/api/jobs", headers=VIEWER)).status == OK
            refused = await h.client.post("/api/jobs/adhoc/trigger", headers=VIEWER)
            assert refused.status == FORBIDDEN
            assert body_of(refused) == {"error": "forbidden"}
            done = await h.client.post("/api/jobs/adhoc/trigger", headers=OPERATOR)
            assert done.status == OK
            me = body_of(await h.client.get(WHOAMI, headers=OPERATOR))
            assert me == {"name": "otto", "role": "operator"}
            typed = await h.client.post(
                "/api/jobs/adhoc/trigger",
                body=b"",
                headers={**OPERATOR, "Content-Type": "text/plain"},
            )
            assert typed.status == UNSUPPORTED_MEDIA

        h.run(body)

    def test_bad_bearer_never_falls_back_to_the_cookie(self, api: MakeApi) -> None:
        """A wrong token is 401 even with a good session cookie alongside it."""
        h = api(users=users_yaml(**PEOPLE))
        log_in(h, "ada")

        async def body() -> None:
            """Send the cookie and a bad token."""
            headers = {"Authorization": "Bearer nope"}
            assert (await h.client.get("/api/jobs", headers=headers)).status == UNAUTHORISED

        h.run(body)


class TestSessions:
    """A logged-in browser is a ``Principal`` too."""

    def test_cookie_session_maps_to_principal(self, api: MakeApi) -> None:
        """After a login the cookie reads the API, and actions are recorded under the user."""
        h = api(tokens=False, users=users_yaml(**PEOPLE))
        _whoami(h)
        csrf = log_in(h, "ola")

        async def body() -> None:
            """Read and act with only the cookie."""
            assert (await h.client.get("/api/runs")).status == OK
            assert body_of(await h.client.get(WHOAMI)) == {"name": "ola", "role": "operator"}
            headers = {**JSON_TYPE, "X-CSRF-Token": csrf}
            made = await h.client.post("/api/jobs/adhoc/trigger", headers=headers)
            assert made.status == OK, made.text
            run_id = body_of(made)["run"]["run_id"]
            detail = body_of(await h.client.get(f"/api/runs/{run_id}"))
            assert any(event["actor"] == "ola" for event in detail["events"])

        h.run(body)

    def test_cookie_post_needs_csrf(self, api: MakeApi) -> None:
        """A cookie POST without ``X-CSRF-Token`` is 403 and does nothing; with it, 200."""
        h = api(users=users_yaml(**PEOPLE))
        csrf = log_in(h, "ola")

        async def body() -> None:
            """Make a waiting run with a token, then hold it with the cookie."""
            made = await h.client.post("/api/jobs/load/trigger", headers=OPERATOR)
            run_id = body_of(made)["run"]["run_id"]
            await h.engine.settle()
            url = f"/api/runs/{run_id}/hold"
            forged = await h.client.post(url, headers=JSON_TYPE)
            assert forged.status == FORBIDDEN
            run = h.engine.book.get(run_id)
            assert run is not None
            assert run.state is not RunState.HELD
            wrong = await h.client.post(url, headers={**JSON_TYPE, "X-CSRF-Token": "nope"})
            assert wrong.status == FORBIDDEN
            held = await h.client.post(url, headers={**JSON_TYPE, "X-CSRF-Token": csrf})
            assert held.status == OK, held.text
            assert body_of(held)["run"]["state"] == "held"

        h.run(body)

    def test_viewer_cookie_cannot_act_and_admin_can(self, api: MakeApi) -> None:
        """A viewer's cookie POST is 403 ``forbidden``; an admin's is allowed."""
        h = api(tokens=False, users=users_yaml(**PEOPLE))
        csrf = log_in(h, "vic")

        async def body() -> None:
            """Trigger as a viewer, then as an admin."""
            headers = {**JSON_TYPE, "X-CSRF-Token": csrf}
            refused = await h.client.post("/api/jobs/adhoc/trigger", headers=headers)
            assert refused.status == FORBIDDEN
            assert body_of(refused) == {"error": "forbidden"}
            admin = {**JSON_TYPE, "X-CSRF-Token": log_in(h, "ada")}
            assert (await h.client.post("/api/jobs/adhoc/trigger", headers=admin)).status == OK

        h.run(body)

    def test_unknown_user_cookie_is_anonymous(self, api: MakeApi) -> None:
        """A signed cookie for a name not in the users file is 401."""
        h = api(tokens=False, users=users_yaml(**PEOPLE))
        log_in(h, "mallory")

        async def body() -> None:
            """Read with the stranger's cookie."""
            assert (await h.client.get("/api/jobs")).status == UNAUTHORISED

        h.run(body)

    def test_reload_rereads_users(self, api: MakeApi) -> None:
        """A user removed from the file is refused right after the reload."""
        h = api(users=users_yaml(**PEOPLE))
        log_in(h, "vic")

        async def body() -> None:
            """Drop ``vic``, reload, and read with the old cookie."""
            assert (await h.client.get("/api/jobs")).status == OK
            users = h.tmp_path / "users.yaml"
            users.write_text(users_yaml(ada="admin"), encoding="utf-8")
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == OK, response.text
            assert (await h.client.get("/api/jobs")).status == UNAUTHORISED

        h.run(body)

    def test_bad_users_file_fails_the_reload(self, api: MakeApi) -> None:
        """A broken users file fails the whole reload; the old users stay."""
        h = api(users=users_yaml(**PEOPLE))
        log_in(h, "vic")

        async def body() -> None:
            """Break the users file, add a job, and reload."""
            (h.tmp_path / "users.yaml").write_text("- just-text\n", encoding="utf-8")
            text = h.config_path.read_text(encoding="utf-8")
            h.config_path.write_text(
                text + "  extra:\n    command: run-extra\n    schedule: manual\n",
                encoding="utf-8",
            )
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == 400  # noqa: PLR2004 -- bad request
            messages = [p["message"] for p in body_of(response)["problems"]]
            assert any("users_file" in m and "mapping" in m for m in messages)
            assert "extra" not in h.engine.config.jobs
            assert (await h.client.get("/api/jobs")).status == OK

        h.run(body)

    def test_users_mode_switch_needs_a_restart(self, api: MakeApi) -> None:
        """Removing ``users_file`` cannot switch the web UI off by a reload."""
        h = api(users=users_yaml(**PEOPLE))

        async def body() -> None:
            """Drop the users line and reload."""
            text = h.config_path.read_text(encoding="utf-8")
            kept = [line for line in text.splitlines() if "users_file" not in line]
            h.config_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == 400  # noqa: PLR2004 -- bad request
            messages = [p["message"] for p in body_of(response)["problems"]]
            assert any("users_file" in m and "restart" in m for m in messages)

        h.run(body)


class TestFailClosed:
    """A caller the access layer did not vouch for is never treated as ``dev``."""

    def test_non_principal_fails_closed(self) -> None:
        """A handler that sees a bare pyweb ``User`` answers 401 ``unauthorised``."""
        app = App()

        @app.middleware
        async def stranger(request: HttpRequest, call_next: CallNext) -> Outcome:
            """Put a plain pyweb user on the request, as a misplaced middleware might."""
            request.user = User("eve", "", frozenset({"admin"}))
            return await call_next(request)

        @app.get("/role")
        @guarded
        async def role(request: HttpRequest) -> dict[str, JsonValue]:
            """Answer the caller's role."""
            return {"role": role_of(request)}

        @app.get("/actor")
        @guarded
        async def actor(request: HttpRequest) -> dict[str, JsonValue]:
            """Answer the caller's name."""
            return {"name": actor_of(request)}

        del stranger, role, actor
        client = TestClient(app)

        async def main() -> None:
            """Ask both routes."""
            for path in ("/role", "/actor"):
                response = await client.get(path)
                assert response.status == UNAUTHORISED
                assert body_of(response) == {"error": "unauthorised"}

        pynet.loop.run(main())


class TestPrincipalOf:
    """Map a pyweb ``User`` to a ``Principal`` by its highest known role."""

    def test_highest_known_role_wins(self) -> None:
        """Take the highest of the known roles, ignore unknown ones, refuse none at all."""
        many = principal_of(User("eve", "", frozenset({"viewer", "admin", "chef"})))
        assert many is not None
        assert many.role == "admin"
        assert principal_of(User("eve", "", frozenset({"chef"}))) is None
        assert principal_of("eve") is None


class TestModes:
    """Development mode, tokens without users, and normal mode."""

    def test_dev_mode_unchanged(self, api: MakeApi) -> None:
        """Loopback callers are ``dev`` with the admin role; a foreign Host is 403."""
        h = api(tokens=False)
        _whoami(h)

        async def body() -> None:
            """Ask as loopback, then under a foreign host, on API, UI and static paths."""
            me = body_of(await h.client.get(WHOAMI, headers=DEV))
            assert me == {"name": "dev", "role": "admin"}
            definition = body_of(await h.client.get("/api/jobs/extract", headers=DEV))
            assert definition["definition"]["alerts"] != "***"
            assert (await h.client.get("/", headers=DEV)).status not in {UNAUTHORISED, FORBIDDEN}
            for path in ("/api/jobs", "/api/health", "/", "/static/app.css", "/login"):
                got = await h.client.get(path, headers={"Host": "evil.example"})
                assert got.status == FORBIDDEN, path
                assert body_of(got) == {"error": "forbidden host"}
            posted = await h.client.post("/", body=b"", headers={"Host": "127.0.0.1"})
            assert posted.status == UNSUPPORTED_MEDIA

        h.run(body)

    def test_tokens_without_users_file(self, api: MakeApi) -> None:
        """UI paths answer 403 ``the web UI needs a users_file``; the API works as before."""
        h = api()

        async def body() -> None:
            """Ask for UI pages, the login page and the API."""
            page = await h.client.get("/")
            assert page.status == FORBIDDEN
            assert body_of(page) == UI_NEEDS_USERS
            assert (await h.client.get("/", headers=VIEWER)).status == FORBIDDEN
            login = await h.client.get("/login", headers=HTML)
            assert login.status == FORBIDDEN
            assert "the web UI needs a users_file" in login.text
            assert "text/html" in (login.headers.get("Content-Type") or "")
            assert (await h.client.get("/api/jobs", headers=VIEWER)).status == OK
            assert (await h.client.get("/api/health")).status == OK
            assert (await h.client.get("/static/app.css")).status not in {UNAUTHORISED, FORBIDDEN}

        h.run(body)

    def test_normal_mode_ui_needs_a_login(self, api: MakeApi) -> None:
        """A stranger on a UI page gets 401, or 303 to the login page in a browser."""
        h = api(users=users_yaml(**PEOPLE))

        async def body() -> None:
            """Ask for UI pages, the login page and static files without a login."""
            assert (await h.client.get("/jobs/extract")).status == UNAUTHORISED
            sent = await h.client.get("/jobs/extract", headers=HTML)
            assert sent.status == SEE_OTHER
            assert sent.headers.get("Location") == "/login?next=/jobs/extract"
            assert (await h.client.get("/login", headers=HTML)).status not in {
                UNAUTHORISED,
                FORBIDDEN,
                SEE_OTHER,
            }
            assert (await h.client.get("/static/app.css")).status not in {UNAUTHORISED, FORBIDDEN}
            log_in(h, "vic")
            assert (await h.client.get("/jobs/extract")).status not in {UNAUTHORISED, FORBIDDEN}
            assert h.engine.book.runs_for("adhoc", TODAY) == []

        h.run(body)


class TestSessionKey:
    """The session secret is made once and kept private."""

    def test_session_key_persists_and_is_private(self, tmp_path: Path) -> None:
        """The key file has mode 0600 and the same key comes back on the next call."""
        state = tmp_path / "state"
        first = load_or_make_key(state)
        path = state / KEY_FILE
        assert stat.S_IMODE(path.stat().st_mode) == PRIVATE
        assert len(first) >= MIN_SECRET
        assert load_or_make_key(state) == first
        assert load_or_make_key(tmp_path / "other") != first

    def test_unusable_key_files_are_session_key_errors(self, tmp_path: Path) -> None:
        """An empty, short or non-ASCII key file raises ``SessionKeyError``, not a traceback."""
        for index, content in enumerate((b"", b"short\n", b"\xff\xfe" * MIN_SECRET)):
            state = tmp_path / f"state{index}"
            state.mkdir()
            (state / KEY_FILE).write_bytes(content)
            with pytest.raises(SessionKeyError):
                load_or_make_key(state)

    def test_no_half_written_key_is_left_behind(self, tmp_path: Path) -> None:
        """A new key arrives whole, and no temporary file is left in the folder."""
        state = tmp_path / "state"
        load_or_make_key(state)
        assert [entry.name for entry in state.iterdir()] == [KEY_FILE]

    def test_build_app_with_a_key_does_no_file_work(self, api: MakeApi) -> None:
        """Given a key, ``build_app`` reads and writes no file; without one, access refuses."""
        h = api(tokens=False, users=users_yaml(ada="admin"))
        state = h.config.settings.state_dir
        (state / KEY_FILE).unlink(missing_ok=True)
        holder = UserHolder(UserTable.load(h.tmp_path / "users.yaml"))
        reload, validate = config_actions(h.engine, h.config_path, users=holder)
        build_app(
            h.engine,
            tokens=None,
            reload=reload,
            validate=validate,
            users=holder,
            session_key="k" * MIN_SECRET,
        )
        assert not (state / KEY_FILE).exists()
        with pytest.raises(ValueError, match="session key"):
            build_access(App(), tokens=TokenHolder(None), users=holder, key=None, secure=False)
        assert needs_session_key(TokenHolder(None), holder)
        assert not needs_session_key(TokenHolder(None), UserHolder(None))
