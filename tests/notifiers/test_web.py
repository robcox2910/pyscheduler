"""Tests for the webhook, Slack and Telegram notifiers."""

import json
import threading
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
