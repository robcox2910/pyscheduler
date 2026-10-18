"""Tests for the email notifier."""

import smtplib
from collections.abc import Mapping
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType

import pynet.loop
import pytest
from pynet.messaging import JsonValue

from pyscheduler.config import ConfigError, load_config
from pyscheduler.notifiers import NotifyError, build_notifier
from pyscheduler.notifiers.email import EmailNotifier

ALERT: Mapping[str, JsonValue] = {"criticality": "high", "message": "nightly late"}
PORT = 2525

calls: list[tuple[str, object]] = []


class FakeSMTP:
    """A recording stand-in for ``smtplib.SMTP``."""

    fail_with: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        """Record the connection."""
        calls.append(("connect", (host, port, timeout)))

    def __enter__(self) -> FakeSMTP:
        """Enter the ``with`` block."""
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Leave the ``with`` block."""

    def starttls(self) -> None:
        """Record the upgrade."""
        calls.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        """Record the login."""
        calls.append(("login", (user, password)))

    def send_message(self, message: EmailMessage) -> None:
        """Record the message (or fail)."""
        if self.fail_with is not None:
            raise self.fail_with
        calls.append(("send_message", message))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``smtplib.SMTP`` with the recorder and clear the call log."""
    calls.clear()
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)


def build(tmp_path: Path, body: str) -> EmailNotifier:
    """Build the notifier ``n`` from a one-notifier config."""
    text = f"notifiers:\n  n: {body}\njobs:\n  a: {{command: 'true', schedule: manual}}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    config = load_config(path, env={})
    notifier = build_notifier(config.notifiers["n"], config.settings)
    assert isinstance(notifier, EmailNotifier)
    return notifier


def test_email_starttls_login_send(tmp_path: Path) -> None:
    """The conversation is connect, starttls, login, send."""
    notifier = build(
        tmp_path,
        f"{{type: email, host: mail.example, port: {PORT}, from: a@x.org, "
        "to: [b@x.org, c@x.org], username: u, password: p}",
    )
    pynet.loop.run(notifier.send(ALERT))
    assert [name for name, _ in calls] == ["connect", "starttls", "login", "send_message"]
    assert calls[0][1] == ("mail.example", PORT, 10.0)
    assert calls[2][1] == ("u", "p")
    message = calls[3][1]
    assert isinstance(message, EmailMessage)
    assert message["Subject"] == "[pysched high] nightly late"
    assert message["To"] == "b@x.org, c@x.org"
    assert message["From"] == "a@x.org"
    assert '"message": "nightly late"' in message.get_content()


def test_email_without_tls_or_login(tmp_path: Path) -> None:
    """A single address, no starttls and no login: just connect and send."""
    notifier = build(
        tmp_path, "{type: email, host: h, from: a@x.org, to: b@x.org, starttls: false}"
    )
    pynet.loop.run(notifier.send(ALERT))
    assert [name for name, _ in calls] == ["connect", "send_message"]


def test_email_server_errors_are_notify_errors(tmp_path: Path) -> None:
    """SMTP and network failures become NotifyError."""
    notifier = build(tmp_path, "{type: email, host: h, from: a@x.org, to: b@x.org}")
    FakeSMTP.fail_with = smtplib.SMTPRecipientsRefused({})
    with pytest.raises(NotifyError):
        pynet.loop.run(notifier.send(ALERT))
    FakeSMTP.fail_with = ConnectionRefusedError("refused")
    with pytest.raises(NotifyError, match="refused"):
        pynet.loop.run(notifier.send(ALERT))


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            "{type: email, host: h, from: a, to: b, username: u}",
            "username and password go together",
        ),
        ("{type: email, host: h, from: a, password: p}", "missing required option 'to'"),
        ("{type: email, host: h, from: a, to: []}", "must be text or a list of text"),
        ("{type: email, host: h, from: a, to: b, port: 0}", "must be a port number"),
        ("{type: email, host: h, from: a, to: b, starttls: maybe}", "true or false"),
    ],
)
def test_email_option_errors(tmp_path: Path, body: str, problem: str) -> None:
    """Bad options are reported when the config loads."""
    text = f"notifiers:\n  n: {body}\njobs:\n  a: {{command: 'true', schedule: manual}}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=problem):
        load_config(path, env={})
