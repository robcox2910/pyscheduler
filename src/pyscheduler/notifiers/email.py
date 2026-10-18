"""The email notifier: a letter through an SMTP post office.

The alert becomes a short email: the subject is the headline, the body is
the full alert as tidy JSON. The conversation with the mail server blocks,
so it runs on a helper thread.
"""

import json
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from typing import cast

from pynet.messaging import JsonValue

from pyscheduler.config.model import NotifierDef, Settings
from pyscheduler.notifiers.base import NotifyError, check_options, text_option
from pyscheduler.notifiers.threads import run_notifier_io

DEFAULT_PORT = 587
MAX_PORT = 65535


def _recipients(definition: NotifierDef) -> tuple[str, ...]:
    """Return the ``to`` option (one address or a list) as a tuple."""
    raw = definition.options["to"]
    if isinstance(raw, str) and raw:
        return (raw,)
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(a, str) and a for a in cast("list[object]", raw))
    ):
        return tuple(cast("list[str]", raw))
    message = "option 'to' for email notifier must be text or a list of text"
    raise ValueError(message)


def _port(definition: NotifierDef) -> int:
    """Return the ``port`` option (default 587)."""
    raw = definition.options.get("port", DEFAULT_PORT)
    if isinstance(raw, str) and raw.isdecimal():
        raw = int(raw)  # config scalars arrive as text
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 < raw <= MAX_PORT:
        message = "option 'port' for email notifier must be a port number"
        raise ValueError(message)
    return raw


def _starttls(definition: NotifierDef) -> bool:
    """Return the ``starttls`` option (default true; ``true``/``false`` text is read)."""
    raw = definition.options.get("starttls", True)
    if isinstance(raw, str) and raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if not isinstance(raw, bool):
        message = "option 'starttls' for email notifier must be true or false"
        raise ValueError(message)  # noqa: TRY004 -- config problems are ValueErrors
    return raw


class EmailNotifier:
    """Send each alert as an email over SMTP."""

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        recipients: tuple[str, ...],
        credentials: tuple[str, str] | None,
        *,
        starttls: bool,
        timeout: float,
    ) -> None:
        """Remember the server, the addresses, the login and the time limit."""
        self.host = host
        self.port = port
        self.sender = sender
        self.recipients = recipients
        self.credentials = credentials
        self.starttls = starttls
        self.timeout = timeout

    @classmethod
    def from_config(cls, definition: NotifierDef, settings: Settings) -> EmailNotifier:
        """Build from ``host``, ``from``, ``to`` and the optional login and TLS settings."""
        del settings
        check_options(
            definition,
            required=("host", "from", "to"),
            optional=("port", "username", "password", "starttls"),
        )
        has_user = "username" in definition.options
        if has_user != ("password" in definition.options):
            message = "username and password go together"
            raise ValueError(message)
        credentials = (
            (text_option(definition, "username"), text_option(definition, "password"))
            if has_user
            else None
        )
        return cls(
            text_option(definition, "host"),
            _port(definition),
            text_option(definition, "from"),
            _recipients(definition),
            credentials,
            starttls=_starttls(definition),
            timeout=definition.timeout.total_seconds(),
        )

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Email the alert: subject ``[pysched <criticality>] <message>``, body its JSON."""
        try:
            mail = self._compose(alert)
            await run_notifier_io(lambda: self._deliver(mail))
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(str(exc) or type(exc).__name__) from exc
        except ValueError as exc:
            message = f"cannot build the email: {exc}"
            raise NotifyError(message) from exc

    def _compose(self, alert: Mapping[str, JsonValue]) -> EmailMessage:
        """Build the email (a subject must be one line, so whitespace is collapsed)."""
        mail = EmailMessage()
        subject = f"[pysched {alert.get('criticality')}] {alert.get('message')}"
        mail["Subject"] = " ".join(subject.split())
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.recipients)
        mail.set_content(json.dumps(alert, indent=2, sort_keys=True))
        return mail

    def _deliver(self, mail: EmailMessage) -> None:
        """Talk to the server (a helper thread)."""
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
            if self.starttls:
                server.starttls(context=ssl.create_default_context())
            if self.credentials is not None:
                server.login(*self.credentials)
            server.send_message(mail)


__all__ = ["EmailNotifier"]
