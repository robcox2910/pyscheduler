"""Test ``scheduler: secure_cookies`` and the start-up warning about Secure cookies (ruling FR3)."""

import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from pyweb.security import hash_password

from pyscheduler.config import ConfigError, load_config
from pyscheduler.service import SECURE_COOKIE_WARNING, session_cookies_secure
from service_helpers import with_service, write_config

OPEN_HOST = "0.0.0.0"  # noqa: S104 -- the off-loopback case under test
PASSWORD = "pw"  # noqa: S105 -- a test user's password
REQUEST_SECONDS = 10.0
SEE_OTHER = 303
WARNING_TEXT = (
    "web sessions use Secure cookies: serve the UI through an HTTPS reverse proxy, "
    "or set secure_cookies: false on a trusted network"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand back a redirect instead of following it."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Do not follow."""
        del args, kwargs


def _users(folder: Path) -> str:
    """Write a users file with one admin and return the ``scheduler:`` lines naming it."""
    users = folder / "users.yaml"
    digest = hash_password(PASSWORD, rounds=1)
    users.write_text(f"- {{name: ada, password_hash: {digest}, role: admin}}\n", "utf-8")
    return f"  users_file: {users}\n"


def _login_cookie(url: str) -> str:
    """Log in as ada and return the ``Set-Cookie`` header of the answer."""
    form = urllib.parse.urlencode({"name": "ada", "password": PASSWORD, "next": "/"}).encode()
    opener = urllib.request.build_opener(_NoRedirect())
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener.open(url + "/login", form, timeout=REQUEST_SECONDS)
    assert caught.value.code == SEE_OTHER
    return caught.value.headers.get("Set-Cookie") or ""


class TestSetting:
    """``secure_cookies`` is an optional yes/no in the ``scheduler:`` block."""

    def test_read(self, tmp_path: Path) -> None:
        """Absent it is ``None`` (decided by the host); ``false`` and ``true`` are read."""
        assert load_config(write_config(tmp_path), env={}).settings.secure_cookies is None
        for text, value in (("false", False), ("true", True)):
            path = write_config(tmp_path, extra=f"  secure_cookies: {text}\n")
            assert load_config(path, env={}).settings.secure_cookies is value

    def test_not_a_bool(self, tmp_path: Path) -> None:
        """Anything but a yes/no is a config error naming the key."""
        path = write_config(tmp_path, extra="  secure_cookies: sometimes\n")
        with pytest.raises(ConfigError, match="secure_cookies"):
            load_config(path, env={})


class TestDecision:
    """``session_cookies_secure`` picks the flag and warns when it may break logins."""

    @pytest.mark.parametrize(
        ("host", "setting", "users", "secure", "warned"),
        [
            ("127.0.0.1", None, True, False, False),
            (OPEN_HOST, None, True, True, True),
            (OPEN_HOST, False, True, False, False),
            (OPEN_HOST, True, True, True, True),
            ("127.0.0.1", True, True, True, False),
            (OPEN_HOST, None, False, True, False),
        ],
    )
    def test_decision(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        *,
        host: str,
        setting: bool | None,
        users: bool,
        secure: bool,
        warned: bool,
    ) -> None:
        """Default: Secure off loopback; the warning only off loopback, Secure, with a web UI."""
        assert SECURE_COOKIE_WARNING == WARNING_TEXT
        extra = "" if setting is None else f"  secure_cookies: {str(setting).lower()}\n"
        extra += _users(tmp_path) if users else ""
        settings = load_config(
            write_config(tmp_path, listen=f"{host}:0", extra=extra), env={}
        ).settings
        caplog.set_level(logging.WARNING, logger="pyscheduler.service")
        assert session_cookies_secure(settings) is secure
        assert (WARNING_TEXT in caplog.messages) is warned


class TestServe:
    """A scheduler listening off loopback over plain HTTP can be told to drop ``Secure``."""

    def test_open_host_secure_by_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Off loopback the cookie is ``Secure`` and the start-up log warns once."""
        caplog.set_level(logging.WARNING, logger="pyscheduler.service")
        path = write_config(tmp_path, listen=f"{OPEN_HOST}:0", extra=_users(tmp_path))
        cookie = with_service(path, _login_cookie)
        assert "Secure" in cookie
        assert caplog.messages.count(WARNING_TEXT) == 1

    def test_open_host_opt_out(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """With ``secure_cookies: false`` the cookie works over plain HTTP; no warning."""
        caplog.set_level(logging.WARNING, logger="pyscheduler.service")
        extra = "  secure_cookies: false\n" + _users(tmp_path)
        path = write_config(tmp_path, listen=f"{OPEN_HOST}:0", extra=extra)
        cookie = with_service(path, _login_cookie)
        assert cookie.startswith("pysched_session=")
        assert "Secure" not in cookie
        assert WARNING_TEXT not in caplog.messages
