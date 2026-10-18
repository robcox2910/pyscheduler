"""The notifiers that post to a web address: webhook, Slack and Telegram.

Each is a postcard dropped in someone else's letterbox: we write the JSON,
post it, and only check that the post office said "delivered" (a 2xx).
The post runs on one of the notifiers' own helper threads, so the scheduler
never waits on the network.
"""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import cast

from pynet.messaging import JsonValue

from pyscheduler.config.model import NotifierDef, Settings
from pyscheduler.notifiers.base import NotifyError, check_options, text_option
from pyscheduler.notifiers.threads import run_notifier_io

TELEGRAM_API = "https://api.telegram.org"
HTTP_SCHEMES = ("http://", "https://")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx answer is a failed delivery."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Refuse every redirect (the 3xx answer then becomes an ``HTTPError``)."""
        del args, kwargs


_OPENER = urllib.request.build_opener(_NoRedirects)
"""The opener every post uses: urllib's usual handlers, minus redirects."""


def _post(url: str, data: bytes, headers: Mapping[str, str], timeout: float) -> None:
    """Post ``data`` and raise ``NotifyError`` unless the answer is 2xx (a helper thread)."""
    request = urllib.request.Request(url, data=data, method="POST")  # noqa: S310 -- http(s) checked
    request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = cast("int", response.status)
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code}"
        raise NotifyError(message) from None
    except urllib.error.URLError as exc:
        raise NotifyError(str(exc.reason)) from exc
    except OSError as exc:
        raise NotifyError(str(exc)) from exc
    except (http.client.HTTPException, ValueError) as exc:
        # Their text can quote the URL (and so a bot token): name the kind only.
        raise NotifyError(type(exc).__name__) from None
    if not 200 <= status < 300:  # noqa: PLR2004 -- the HTTP success range
        message = f"HTTP {status}"
        raise NotifyError(message)


async def post_json(
    url: str, body: JsonValue, *, headers: Mapping[str, str], timeout: float
) -> None:
    """POST ``body`` as JSON to ``url`` without blocking the loop.

    Raises:
        NotifyError: On a non-2xx answer (including an unfollowed redirect)
            or a network error.

    """
    data = json.dumps(body, sort_keys=True).encode()
    await run_notifier_io(lambda: _post(url, data, headers, timeout))


def _plain(definition: NotifierDef, key: str, value: str, *, spaces: bool = False) -> str:
    """Return ``value`` unless it holds a control character (or a space, unless ``spaces``)."""
    if any(ord(c) < ord(" ") or ord(c) == ord("\x7f") or (c == " " and not spaces) for c in value):
        message = f"option {key!r} for {definition.type} notifier must not hold spaces or control characters"
        raise ValueError(message)
    return value


def _url_option(definition: NotifierDef, key: str, default: str | None = None) -> str:
    """Return option ``key`` as an http(s) address."""
    value = _plain(definition, key, text_option(definition, key, default))
    if not value.startswith(HTTP_SCHEMES):
        message = (
            f"option {key!r} for {definition.type} notifier must start with http:// or https://"
        )
        raise ValueError(message)
    return value


def _headers_option(definition: NotifierDef) -> dict[str, str]:
    """Return the ``headers`` option as a text-to-text map (empty when absent)."""
    raw = definition.options.get("headers", {})
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in cast("dict[object, object]", raw).items()
    ):
        message = "option 'headers' for webhook notifier must be a map of text to text"
        raise ValueError(message)
    headers = cast("dict[str, str]", raw)
    for name, value in headers.items():
        _plain(definition, "headers", name)
        _plain(definition, "headers", value, spaces=True)
    return dict(headers)


def _line(alert: Mapping[str, JsonValue]) -> str:
    """Return the one-line text chat notifiers send: ``[criticality] message``."""
    return f"[{alert.get('criticality')}] {alert.get('message')}"


class WebhookNotifier:
    """Post the alert JSON to a URL."""

    def __init__(self, url: str, headers: Mapping[str, str], timeout: float) -> None:
        """Remember where to post, the extra headers, and the time limit."""
        self._url = url
        self.headers = dict(headers)
        self.timeout = timeout

    @classmethod
    def from_config(cls, definition: NotifierDef, settings: Settings) -> WebhookNotifier:
        """Build from ``url`` and the optional ``headers`` map."""
        del settings
        check_options(definition, required=("url",), optional=("headers",))
        return cls(
            _url_option(definition, "url"),
            _headers_option(definition),
            definition.timeout.total_seconds(),
        )

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Post the alert as it is."""
        await post_json(self._url, dict(alert), headers=self.headers, timeout=self.timeout)


class SlackNotifier:
    """Post ``[criticality] message`` to a Slack incoming webhook."""

    def __init__(self, url: str, timeout: float) -> None:
        """Remember the webhook address and the time limit."""
        self._url = url
        self.timeout = timeout

    @classmethod
    def from_config(cls, definition: NotifierDef, settings: Settings) -> SlackNotifier:
        """Build from ``webhook_url``."""
        del settings
        check_options(definition, required=("webhook_url",))
        return cls(_url_option(definition, "webhook_url"), definition.timeout.total_seconds())

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Post the one-line text."""
        await post_json(self._url, {"text": _line(alert)}, headers={}, timeout=self.timeout)


class TelegramNotifier:
    """Send ``[criticality] message`` through a Telegram bot."""

    def __init__(self, api_url: str, bot_token: str, chat_id: str, timeout: float) -> None:
        """Remember the bot, the chat, the API address and the time limit."""
        self._url = f"{api_url.rstrip('/')}/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    @classmethod
    def from_config(cls, definition: NotifierDef, settings: Settings) -> TelegramNotifier:
        """Build from ``bot_token``, ``chat_id`` and the optional ``api_url``."""
        del settings
        check_options(definition, required=("bot_token", "chat_id"), optional=("api_url",))
        raw_chat = definition.options["chat_id"]
        chat_id = (
            str(raw_chat) if isinstance(raw_chat, int) and not isinstance(raw_chat, bool) else None
        )
        if chat_id is None:
            chat_id = text_option(definition, "chat_id")
        return cls(
            _url_option(definition, "api_url", TELEGRAM_API),
            _plain(definition, "bot_token", text_option(definition, "bot_token")),
            chat_id,
            definition.timeout.total_seconds(),
        )

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Send the one-line text to the chat."""
        body: JsonValue = {"chat_id": self.chat_id, "text": _line(alert)}
        await post_json(self._url, body, headers={}, timeout=self.timeout)


__all__ = ["SlackNotifier", "TelegramNotifier", "WebhookNotifier", "post_json"]
