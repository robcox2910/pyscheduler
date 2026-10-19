"""Bearer tokens: the wristbands that let callers into the API.

At a festival you show your wristband at the gate. A green band lets
you watch the shows (a **viewer**); a gold band also lets you onto the
stage to move the lights (an **operator**). The steward never keeps a
copy of your band: they keep a *fingerprint* of it (a SHA-256 hash), so
a stolen guest list opens no gates.

The tokens file is a YAML list::

    - {name: ci, token_sha256: 3f5a..., role: operator}
    - {name: dashboard, token_sha256: 9b1c..., role: viewer}

Callers send ``Authorization: Bearer <token>``. Reads (``GET``) need a
viewer or operator; everything else needs an operator. ``/api/health``
needs no token at all.

With no tokens file and no users file the API is in **development
mode**: every caller is ``dev`` with the admin role. ``serve`` only allows that on a loopback
address, but loopback is *not* private: every program on this machine,
including a web browser visiting some other site, can send it requests.
Two guards keep a web page from steering the scheduler:

- every ``POST`` must carry ``Content-Type: application/json`` (in every
  mode), which a plain cross-site form or ``fetch`` cannot send without
  the browser asking the server first (and we never say yes);
- in development mode the ``Host`` header must be a loopback name
  (``localhost``, ``127.x.x.x`` or ``[::1]``, with an optional port), so
  a page that re-points its own domain at ``127.0.0.1`` (DNS rebinding)
  is refused too.

The tokens can be swapped while the API runs (``TokenHolder.table``),
so a reload can revoke a token without a restart. The checks themselves
live in ``pyscheduler.api.access``, which serves tokens and web sessions
alike.
"""

import hashlib
import hmac
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, Scalar, Seq, YamlError, parse

__all__ = [
    "DEV_PRINCIPAL",
    "HEALTH_PATH",
    "READ_METHODS",
    "ROLE_RANK",
    "Principal",
    "Role",
    "SwapNotices",
    "TokenFileError",
    "TokenHolder",
    "TokenTable",
    "allows",
    "is_loopback_host",
]

type Role = Literal["viewer", "operator", "admin"]

ROLE_RANK: dict[Role, int] = {"viewer": 0, "operator": 1, "admin": 2}
"""How far up the ladder each role stands: ``admin`` >= ``operator`` >= ``viewer``."""

HEALTH_PATH = "/api/health"
"""The one path that needs no token."""

READ_METHODS = frozenset({"GET", "HEAD"})
"""Methods a viewer may use; any other method needs an operator."""

_FIELDS = ("name", "token_sha256", "role")
_HEX_DIGITS = frozenset("0123456789abcdef")
_SHA256_HEX_LEN = 64


@dataclass(frozen=True)
class Principal:
    """Who is calling: the token's name and its role."""

    name: str
    role: Role


DEV_PRINCIPAL = Principal("dev", "admin")
"""The caller of every request in development mode (no tokens file, no users file)."""


def allows(role: Role, needed: Role) -> bool:
    """Return whether ``role`` may do what needs ``needed`` (a higher role may do more)."""
    return ROLE_RANK[role] >= ROLE_RANK[needed]


class TokenFileError(ValueError):
    """Raised when the tokens file cannot be read or is malformed."""


@dataclass(frozen=True)
class _Entry:
    """One token: its name, the hex SHA-256 of the token, and its role."""

    name: str
    digest: str
    role: Role


def _text(node: Node | None, where: str, field: str) -> str:
    """Return the scalar text of ``field``, or raise ``TokenFileError``."""
    if node is None:
        msg = f"{where}: missing {field!r}"
        raise TokenFileError(msg)
    if not isinstance(node, Scalar):
        msg = f"{where}: {field!r} must be text"
        raise TokenFileError(msg)
    return node.value


def _entry(item: Node, filename: str) -> _Entry:
    """Check one list item and turn it into an ``_Entry``."""
    where = f"{filename}:{item.line}"
    if not isinstance(item, Map):
        msg = f"{where}: each token must be a mapping of {', '.join(_FIELDS)}"
        raise TokenFileError(msg)
    unknown = sorted(set(item.items) - set(_FIELDS))
    if unknown:
        msg = f"{where}: unknown field {unknown[0]!r}"
        raise TokenFileError(msg)
    name = _text(item.items.get("name"), where, "name")
    try:
        check_name(name)
    except ValueError as exc:
        msg = f"{where}: bad name: {exc}"
        raise TokenFileError(msg) from None
    digest = _text(item.items.get("token_sha256"), where, "token_sha256")
    if len(digest) != _SHA256_HEX_LEN or not set(digest) <= _HEX_DIGITS:
        msg = f"{where}: token_sha256 must be 64 lowercase hex digits"
        raise TokenFileError(msg)
    role = _text(item.items.get("role"), where, "role")
    if role == "viewer":
        return _Entry(name, digest, "viewer")
    if role == "operator":
        return _Entry(name, digest, "operator")
    msg = f"{where}: role must be viewer or operator, not {role!r}"
    raise TokenFileError(msg)


class TokenTable:
    """Hold the known tokens (as hashes) and look callers up."""

    def __init__(self, entries: tuple[_Entry, ...]) -> None:
        """Keep the checked entries; use ``load`` to read a file."""
        self._entries = entries

    @classmethod
    def load(cls, path: Path) -> TokenTable:
        """Read the tokens file at ``path``.

        Raises:
            TokenFileError: If the file cannot be read, is not a list of
                ``{name, token_sha256, role}`` mappings, or repeats a name
                or a hash. The message names the file and line.

        """
        filename = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"{filename}: cannot read tokens file: {exc}"
            raise TokenFileError(msg) from None
        try:
            root = parse(text, filename)
        except YamlError as exc:
            raise TokenFileError(str(exc)) from None
        if not isinstance(root, Seq):
            msg = f"{filename}:{root.line}: the tokens file must be a list of tokens"
            raise TokenFileError(msg)
        entries = tuple(_entry(item, filename) for item in root.items)
        names = [e.name for e in entries]
        digests = [e.digest for e in entries]
        for seen, what in ((names, "name"), (digests, "token_sha256")):
            repeated = sorted({value for value in seen if seen.count(value) > 1})
            if repeated:
                msg = f"{filename}: the same {what} is listed twice"
                raise TokenFileError(msg)
        return cls(entries)

    def lookup(self, token: str) -> tuple[str, Role] | None:
        """Return ``(name, role)`` for ``token``, or ``None`` if it is unknown.

        Every entry is compared with ``hmac.compare_digest``, so the time
        taken does not hint at how close a guess was.
        """
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        found: tuple[str, Role] | None = None
        for entry in self._entries:
            if hmac.compare_digest(entry.digest, digest):
                found = (entry.name, entry.role)
        return found


class SwapNotices:
    """The callbacks to tell when a holder's table is swapped by a reload.

    The web UI listens here to end its open streams, so a revoked token
    or a removed user cannot keep one open (it is checked again when the
    browser reconnects).
    """

    def __init__(self) -> None:
        """Start with nobody to tell."""
        self._callbacks: list[Callable[[], None]] = []

    def watch(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` after every swap."""
        self._callbacks.append(callback)

    def tell(self) -> None:
        """Call every watcher, in the order they were added."""
        for callback in list(self._callbacks):
            callback()


@dataclass
class TokenHolder:
    """Hold the current token table (``None`` means development mode).

    The middleware reads ``table`` on every request, so replacing it
    (a reload re-reading the tokens file) takes effect at once. ``swap``
    replaces it and tells ``swapped``.
    """

    table: TokenTable | None
    swapped: SwapNotices = field(default_factory=SwapNotices, repr=False, compare=False)

    def swap(self, table: TokenTable | None) -> None:
        """Put ``table`` in force, then tell the watchers."""
        self.table = table
        self.swapped.tell()


def is_loopback_host(header: str | None) -> bool:
    """Return whether a ``Host`` header names this machine.

    ``localhost``, ``127.0.0.0/8`` and ``[::1]`` count, each with an
    optional ``:port``; anything else (or no header) does not.
    """
    if not header:
        return False
    text = header.strip()
    if text.startswith("["):
        name, sep, rest = text[1:].partition("]")
        if not sep:
            return False
    else:
        name, sep, port = text.partition(":")
        rest = sep + port
    if rest and not (rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit()):
        return False
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False
