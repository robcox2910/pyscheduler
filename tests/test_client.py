"""Tests for ``ApiClient`` against small stand-in servers that misbehave."""

import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyscheduler.client import BAD_ANSWER, UNREACHABLE, ApiClient, ApiError, safe_url

FOUND = 302
SHORT_TIMEOUT = 0.2
SLOW = 1.0
POLL_INTERVAL = 0.01

type Reply = Callable[[BaseHTTPRequestHandler], None]


def _redirect(handler: BaseHTTPRequestHandler) -> None:
    """Send the caller somewhere else."""
    handler.send_response(FOUND)
    handler.send_header("Location", "http://user:pw@elsewhere.example/api/health")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _garbage(handler: BaseHTTPRequestHandler) -> None:
    """Answer 200 with text that is not JSON."""
    body = b"<html>hello</html>"
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _slow(handler: BaseHTTPRequestHandler) -> None:
    """Take longer than the client waits."""
    time.sleep(SLOW)
    _garbage(handler)


def _hang_up(handler: BaseHTTPRequestHandler) -> None:
    """Close the connection without an answer."""
    handler.close_connection = True


@pytest.fixture
def stub() -> Iterator[Callable[[Reply], str]]:
    """Return a factory: start a server that answers every GET with ``reply``; return its URL."""
    servers: list[ThreadingHTTPServer] = []

    def start(reply: Reply) -> str:
        """Start one stand-in server on a free port."""

        class Handler(BaseHTTPRequestHandler):
            """Answer with ``reply``."""

            def do_GET(self) -> None:
                """Answer a GET."""
                reply(self)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                """Keep quiet."""
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, args=(POLL_INTERVAL,), daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


class TestMisbehavingServers:
    """Each kind of trouble gets its own status and message."""

    def test_redirect_is_not_followed(self, stub: Callable[[Reply], str]) -> None:
        """A redirect is refused (so the token is not sent on), without the userinfo."""
        with pytest.raises(ApiError) as caught:
            ApiClient(stub(_redirect), "secret").health()
        assert caught.value.status == FOUND
        assert "not followed" in caught.value.message
        assert "pw" not in caught.value.message

    def test_not_json(self, stub: Callable[[Reply], str]) -> None:
        """A 200 that is not a JSON object is ``BAD_ANSWER``."""
        with pytest.raises(ApiError) as caught:
            ApiClient(stub(_garbage), None).health()
        assert caught.value.status == BAD_ANSWER

    def test_timeout(self, stub: Callable[[Reply], str]) -> None:
        """A slow answer says how long the client waited."""
        with pytest.raises(ApiError, match=r"did not answer within 0\.2s") as caught:
            ApiClient(stub(_slow), None, timeout=SHORT_TIMEOUT).health()
        assert caught.value.status == UNREACHABLE

    def test_hang_up(self, stub: Callable[[Reply], str]) -> None:
        """A connection closed without an answer is ``BAD_ANSWER``."""
        with pytest.raises(ApiError) as caught:
            ApiClient(stub(_hang_up), None).health()
        assert caught.value.status == BAD_ANSWER


class TestSafeUrl:
    """``safe_url`` drops the userinfo from URLs shown in messages."""

    def test_strips_userinfo(self) -> None:
        """Keep the scheme, host, port and path."""
        assert safe_url("http://bob:pw@host:8470/x") == "http://host:8470/x"
        assert safe_url("http://host:8470") == "http://host:8470"

    def test_unreachable_message_has_no_password(self) -> None:
        """A connection error names the server without its password."""
        with pytest.raises(ApiError) as caught:
            ApiClient("http://bob:pw@127.0.0.1:1", None).health()
        assert caught.value.status == UNREACHABLE
        assert caught.value.message == "cannot reach scheduler at http://127.0.0.1:1"


class TestPostHeaders:
    """Every POST says its body is JSON, even when there is no body (ruling R24)."""

    def test_bodyless_post_sends_json_type(self) -> None:
        """``reload`` (no body) sends ``Content-Type: application/json``."""
        seen: list[str | None] = []

        class Handler(BaseHTTPRequestHandler):
            """Record the Content-Type of each POST and answer ``{}``."""

            def do_POST(self) -> None:
                """Answer a POST."""
                seen.append(self.headers.get("Content-Type"))
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                """Keep quiet."""
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, args=(POLL_INTERVAL,), daemon=True).start()
        try:
            client = ApiClient(f"http://127.0.0.1:{server.server_address[1]}", None)
            client.reload()
            client.run_action("a.2026-10-19.1", "hold")
        finally:
            server.shutdown()
            server.server_close()
        assert seen == ["application/json", "application/json"]
