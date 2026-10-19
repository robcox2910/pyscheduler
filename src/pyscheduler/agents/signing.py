"""Sign and check agent messages, like a wax seal on a letter.

The broker is a post office with no door guards: anyone who can reach it
can drop a letter in. So every letter between the scheduler and an agent
carries a wax seal made with a secret only those two share. The seal is
an HMAC-SHA256 over the letter's canonical JSON. A reader who gets a
letter checks four things before trusting it: the seal fits, the date is
fresh, the letter's serial number (its *nonce*) was never seen before,
and the sender is somebody it knows. Anything else is refused, and the
refusal says why but never repeats the secret or the contents.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from pynet.messaging import JsonValue

from pyscheduler.engine.clock import Clock

__all__ = [
    "MAX_SKEW",
    "NONCE_TTL",
    "SIG_HEADER",
    "Message",
    "Rejected",
    "Verifier",
    "seal",
    "sign",
]

MAX_SKEW = timedelta(minutes=5)
"""How far a message's ``sent_at`` may be from our clock, in either direction."""
NONCE_TTL = timedelta(minutes=10)
"""How long a seen nonce is remembered (twice the skew, so a replay is always caught)."""
SIG_HEADER = "sig"
"""The header that carries the hex digest."""
NONCE_BYTES = 16
# The short reasons a message can be refused; none holds a secret or payload.
NOT_OBJECT = "not an object"
MALFORMED = "malformed"
MISSING_SIG = "missing sig"
UNKNOWN_AGENT = "unknown agent"
BAD_SIGNATURE = "bad signature"
STALE = "stale"
REPLAYED = "replayed nonce"
_FIELDS = ("type", "agent", "sent_at", "nonce", "payload")


class Rejected(Exception):
    """Raised when a message fails a check; the text is a short reason.

    The reason never holds the secret or any of the payload.
    """

    def __init__(self, reason: str) -> None:
        """Remember the short human ``reason``."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Message:
    """A message that passed every check."""

    kind: str
    agent: str
    sent_at: datetime
    payload: Mapping[str, JsonValue]


def sign(body: Mapping[str, JsonValue], secret: str) -> str:
    """Return the hex HMAC-SHA256 of ``body``'s canonical JSON under ``secret``."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def seal(
    kind: str,
    agent: str,
    payload: Mapping[str, JsonValue],
    secret: str,
    *,
    now: datetime,
) -> tuple[dict[str, JsonValue], dict[str, str]]:
    """Return the body and headers (with ``sig``) for a message of ``kind``.

    Args:
        kind: The message type, such as ``"work"`` or ``"heartbeat"``.
        agent: The agent the message is to or from.
        payload: The message's own content.
        secret: The agent's shared secret.
        now: The send time; must be aware.

    """
    body: dict[str, JsonValue] = {
        "type": kind,
        "agent": agent,
        "sent_at": now.isoformat(),
        "nonce": secrets.token_hex(NONCE_BYTES),
        "payload": dict(payload),
    }
    return body, {SIG_HEADER: sign(body, secret)}


def _fields(body: dict[str, JsonValue]) -> tuple[str, str, str, str, Mapping[str, JsonValue]]:
    """Return the checked envelope fields of ``body`` or raise ``Rejected``."""
    kind, agent, sent_at, nonce, payload = (body.get(key) for key in _FIELDS)
    if not (
        isinstance(kind, str)
        and isinstance(agent, str)
        and isinstance(sent_at, str)
        and isinstance(nonce, str)
        and isinstance(payload, dict)
    ):
        raise Rejected(MALFORMED)
    return kind, agent, sent_at, nonce, payload


def _parse_time(text: str) -> datetime:
    """Return the aware instant in ``text`` or raise ``Rejected``."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise Rejected(MALFORMED) from None
    if when.tzinfo is None:
        raise Rejected(MALFORMED)
    return when


class Verifier:
    """Check incoming messages and remember the nonces already seen.

    Args:
        secrets: Look up an agent's secret; ``None`` means unknown.
        clock: The clock that judges freshness and ages out nonces.

    """

    def __init__(self, secrets: Callable[[str], str | None], *, clock: Clock) -> None:
        """Start with no nonces seen."""
        self._secrets = secrets
        self._clock = clock
        self._seen: dict[str, datetime] = {}

    @property
    def remembered(self) -> int:
        """Return how many nonces are remembered right now."""
        return len(self._seen)

    def check(self, body: JsonValue, headers: Mapping[str, str]) -> Message:
        """Return the verified ``Message`` or raise ``Rejected(reason)``."""
        now = self._clock.now()
        self._forget(now)
        if not isinstance(body, dict):
            raise Rejected(NOT_OBJECT)
        kind, agent, sent_at, nonce, payload = _fields(body)
        sig = headers.get(SIG_HEADER)
        if sig is None:
            raise Rejected(MISSING_SIG)
        secret = self._secrets(agent)
        if secret is None:
            raise Rejected(UNKNOWN_AGENT)
        expected = sign(body, secret)
        if not hmac.compare_digest(expected.encode("ascii"), sig.encode("utf-8")):
            raise Rejected(BAD_SIGNATURE)
        when = _parse_time(sent_at)
        if abs(now - when) > MAX_SKEW:
            raise Rejected(STALE)
        if nonce in self._seen:
            raise Rejected(REPLAYED)
        self._seen[nonce] = now + NONCE_TTL
        return Message(kind, agent, when, payload)

    def _forget(self, now: datetime) -> None:
        """Drop nonces whose time is up."""
        for nonce in [n for n, until in self._seen.items() if until <= now]:
            del self._seen[nonce]
