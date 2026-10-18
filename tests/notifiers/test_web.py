"""Tests for the webhook, Slack and Telegram notifiers."""

import http.client
import json
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pynet.loop
import pytest
from pynet.messaging import JsonValue

from pyscheduler.config import ConfigError, load_config
from pyscheduler.notifiers import NotifyError, build_notifier
from pyscheduler.notifiers.web import SlackNotifier, TelegramNotifier, WebhookNotifier

ALERT: Mapping[str, JsonValue] = {
    "alert_id": "load.2026-10-19.1:failed:1",
    "criticality": "critical",
    "message": "load.2026-10-19.1 failed: exit 1",
}
SERVER_ERROR = 500
SLOW_LIMIT_SECONDS = 3.0
TICK_SECONDS = 0.05
MIN_TICKS = 5
STUCK_SENDS = 4
STUCK_TIMEOUT = "2s"
SHARED_THREAD_LIMIT = 0.2


@dataclass
class Seen:
    """What the test server was asked."""

    requests: list[tuple[str, dict[str, str], bytes]] = field(default_factory=lambda: [])
    release: threading.Event = field(default_factory=threading.Event)


def make_handler(seen: Seen) -> type[BaseHTTPRequestHandler]:
    """Return a handler class that records posts and answers by path."""

    class Handler(BaseHTTPRequestHandler):
        """Record the post, then answer 200, 500, 302 or never."""

        def do_POST(self) -> None:
            """Handle one post."""
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.requests.append((self.path, dict(self.headers.items()), body))
            if self.path == "/hang":
                seen.release.wait(10)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
            elif self.path == "/error":
                self.send_response(SERVER_ERROR)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Stay quiet."""
            del format, args

    return Handler


@pytest.fixture
def server() -> Iterator[tuple[str, Seen]]:
    """Run a test HTTP server; yield its base URL and the record of requests."""
    seen = Seen()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(seen))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", seen
    seen.release.set()
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def build(tmp_path: Path, body: str) -> WebhookNotifier | SlackNotifier | TelegramNotifier:
    """Build the notifier ``n`` from a one-notifier config."""
    text = f"notifiers:\n  n: {body}\njobs:\n  a: {{command: 'true', schedule: manual}}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    config = load_config(path, env={})
    notifier = build_notifier(config.notifiers["n"], config.settings)
    assert isinstance(notifier, WebhookNotifier | SlackNotifier | TelegramNotifier)
    return notifier


def test_webhook_posts_alert_json(tmp_path: Path, server: tuple[str, Seen]) -> None:
    """The body is the alert, as JSON, with the custom header."""
    url, seen = server
    notifier = build(tmp_path, f"{{type: webhook, url: '{url}/ok', headers: {{X-Token: abc}}}}")
    pynet.loop.run(notifier.send(ALERT))
    path, headers, body = seen.requests[0]
    assert path == "/ok"
    assert json.loads(body) == ALERT
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Token"] == "abc"


def test_slack_body(tmp_path: Path, server: tuple[str, Seen]) -> None:
    """Slack gets one line of text."""
    url, seen = server
    notifier = build(tmp_path, f"{{type: slack, webhook_url: '{url}/hook'}}")
    pynet.loop.run(notifier.send(ALERT))
    assert json.loads(seen.requests[0][2]) == {
        "text": "[critical] load.2026-10-19.1 failed: exit 1"
    }


def test_telegram_url_and_body(tmp_path: Path, server: tuple[str, Seen]) -> None:
    """Telegram posts to the bot's sendMessage path with the chat id."""
    url, seen = server
    notifier = build(
        tmp_path, f"{{type: telegram, bot_token: TOKEN, chat_id: '42', api_url: '{url}'}}"
    )
    pynet.loop.run(notifier.send(ALERT))
    path, _, body = seen.requests[0]
    assert path == "/botTOKEN/sendMessage"
    assert json.loads(body) == {
        "chat_id": "42",
        "text": "[critical] load.2026-10-19.1 failed: exit 1",
    }


def test_telegram_numeric_chat_id(tmp_path: Path, server: tuple[str, Seen]) -> None:
    """A numeric chat id is sent as text."""
    url, seen = server
    notifier = build(tmp_path, f"{{type: telegram, bot_token: T, chat_id: 42, api_url: '{url}'}}")
    pynet.loop.run(notifier.send(ALERT))
    assert json.loads(seen.requests[0][2])["chat_id"] == "42"


def test_http_error_status(tmp_path: Path, server: tuple[str, Seen]) -> None:
    """A 500 fails, and a 302 is not followed but fails too."""
    url, seen = server
    notifier = build(tmp_path, f"{{type: webhook, url: '{url}/error'}}")
    with pytest.raises(NotifyError) as caught:
        pynet.loop.run(notifier.send(ALERT))
    assert str(caught.value) == "HTTP 500"
    notifier = build(tmp_path, f"{{type: webhook, url: '{url}/redirect'}}")
    with pytest.raises(NotifyError) as caught:
        pynet.loop.run(notifier.send(ALERT))
    assert str(caught.value) == "HTTP 302"
    assert [r[0] for r in seen.requests] == ["/error", "/redirect"]


def test_connection_refused_is_a_notify_error(tmp_path: Path) -> None:
    """Nobody listening is a failed send, not a traceback."""
    notifier = build(tmp_path, "{type: webhook, url: 'http://127.0.0.1:1/x'}")
    with pytest.raises(NotifyError):
        pynet.loop.run(notifier.send(ALERT))


def test_http_timeout(tmp_path: Path, server: tuple[str, Seen]) -> None:
    """A silent server fails within the timeout while the loop keeps ticking."""
    url, _ = server
    notifier = build(tmp_path, f"{{type: webhook, timeout: 1s, url: '{url}/hang'}}")
    ticks = 0

    async def main() -> float:
        """Send while a ticker runs; return the seconds the send took."""
        nonlocal ticks

        async def ticker() -> None:
            """Count 50 ms ticks."""
            nonlocal ticks
            while True:
                await pynet.loop.sleep(TICK_SECONDS)
                ticks += 1

        task = pynet.loop.spawn(ticker(), name="ticker")
        loop = pynet.loop.get_running_loop()
        started = loop.time()
        try:
            await notifier.send(ALERT)
        except NotifyError:
            return loop.time() - started
        finally:
            task.cancel()
        return -1.0

    elapsed = pynet.loop.run(main())
    assert 0 < elapsed < SLOW_LIMIT_SECONDS
    assert ticks >= MIN_TICKS


def test_stuck_sends_leave_the_shared_threads_free(tmp_path: Path) -> None:
    """Four sends to a server that never answers do not hold up the loop's helper threads.

    The store writer commits on those shared threads; notifiers have a team
    of their own, so a trivial ``run_in_thread`` still returns at once.
    """
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(STUCK_SENDS * 2)
        port = silent.getsockname()[1]
        notifier = build(
            tmp_path,
            f"{{type: webhook, timeout: {STUCK_TIMEOUT}, url: 'http://127.0.0.1:{port}/x'}}",
        )

        async def main() -> float:
            """Start the stuck sends, then time a trivial shared-thread job."""

            async def one() -> None:
                """Send once and expect the timeout."""
                with pytest.raises(NotifyError):
                    await notifier.send(ALERT)

            sends = [pynet.loop.spawn(one(), name=f"send-{i}") for i in range(STUCK_SENDS)]
            await pynet.loop.sleep(TICK_SECONDS * 2)
            started = time.monotonic()
            await pynet.loop.run_in_thread(lambda: None)
            waited = time.monotonic() - started
            await pynet.loop.gather(*sends)
            return waited

        assert pynet.loop.run(main()) < SHARED_THREAD_LIMIT


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("{type: webhook}", "missing required option 'url' for webhook notifier"),
        ("{type: webhook, url: 'ftp://x'}", "must start with http"),
        ("{type: webhook, url: 'http://x', headers: [a]}", "must be a map of text to text"),
        ("{type: slack, webhook_url: 'http://x', extra: 1}", "unknown option 'extra'"),
        ("{type: telegram, bot_token: T}", "missing required option 'chat_id'"),
    ],
)
def test_option_errors(tmp_path: Path, body: str, problem: str) -> None:
    """Bad options are reported at validation time."""
    text = f"notifiers:\n  n: {body}\njobs:\n  a: {{command: 'true', schedule: manual}}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=problem):
        load_config(path, env={})


SECRET = "123:SECRET"  # noqa: S105 -- a fake token


@pytest.mark.parametrize(
    "body",
    [
        "{type: telegram, bot_token: \"1\\n2\", chat_id: '4'}",
        '{type: webhook, url: "http://x/a\\nb"}',
        "{type: webhook, url: 'http://x', headers: {X: \"a\\r\\nB: c\"}}",
        "{type: slack, webhook_url: 'http://x/a b'}",
    ],
)
def test_control_characters_are_config_problems(tmp_path: Path, body: str) -> None:
    """Newlines and spaces in addresses, tokens and header values fail at load time."""
    text = f"notifiers:\n  n: {body}\njobs:\n  a: {{command: 'true', schedule: manual}}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="must not hold spaces or control characters"):
        load_config(path, env={})


def test_http_exceptions_never_leak_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An odd protocol error becomes a NotifyError naming only its kind."""

    class Boom:
        """An opener whose open fails with a message quoting the URL."""

        def open(self, request: urllib.request.Request, timeout: float) -> None:
            """Fail like http.client does."""
            del timeout
            message = f"bad {request.full_url}"
            raise http.client.InvalidURL(message)

    monkeypatch.setattr("pyscheduler.notifiers.web._OPENER", Boom())
    notifier = build(
        tmp_path, f"{{type: telegram, bot_token: '{SECRET}', chat_id: '4', api_url: 'http://x'}}"
    )
    with pytest.raises(NotifyError) as caught:
        pynet.loop.run(notifier.send(ALERT))
    assert str(caught.value) == "InvalidURL"
    assert SECRET not in repr(notifier)
