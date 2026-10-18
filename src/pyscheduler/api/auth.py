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

With no tokens file the API is in **development mode**: every caller is
``dev`` with the operator role. ``serve`` only allows that on a loopback
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
so a reload can revoke a token without a restart.
"""

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pyweb.app import CallNext, Middleware
from pyweb.messages import HttpRequest, Outcome, json

from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, Scalar, Seq, YamlError, parse

__all__ = [
    "DEV_PRINCIPAL",
    "HEALTH_PATH",
    "Principal",
    "Role",
    "TokenFileError",
    "TokenHolder",
    "TokenTable",
    "is_loopback_host",
    "token_middleware",
]

type Role = Literal["viewer", "operator"]

HEALTH_PATH = "/api/health"
"""The one path that needs no token."""

READ_METHODS = frozenset({"GET", "HEAD"})
"""Methods a viewer may use; any other method needs an operator."""

_FIELDS = ("name", "token_sha256", "role")
_HEX_DIGITS = frozenset("0123456789abcdef")
_SHA256_HEX_LEN = 64
_UNAUTHORISED = 401
_FORBIDDEN = 403
_UNSUPPORTED_MEDIA = 415
_BEARER = "bearer"
_JSON_TYPE = "application/json"


@dataclass(frozen=True)
class Principal:
    """Who is calling: the token's name and its role."""

    name: str
    role: Role


DEV_PRINCIPAL = Principal("dev", "operator")
"""The caller of every request in development mode (no tokens file)."""


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


@dataclass
class TokenHolder:
    """Hold the current token table (``None`` means development mode).

    The middleware reads ``table`` on every request, so replacing it
    (a reload re-reading the tokens file) takes effect at once.
    """

    table: TokenTable | None


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


def _is_json(request: HttpRequest) -> bool:
    """Return whether the request says its body is JSON (a charset may follow)."""
    content_type = request.headers.get("Content-Type") or ""
    return content_type.partition(";")[0].strip().lower() == _JSON_TYPE


def _bearer(request: HttpRequest) -> str | None:
    """Return the token of an ``Authorization: Bearer`` header, or ``None``."""
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != _BEARER or not token:
        return None
    return token


def _unauthorised() -> Outcome:
    """Build the 401 answer that asks for a bearer token."""
    response = json({"error": "unauthorised"}, _UNAUTHORISED)
    response.headers.set("WWW-Authenticate", "Bearer")
    return response


def token_middleware(tokens: TokenTable | TokenHolder | None) -> Middleware:
    """Return middleware that sets ``request.user`` to a ``Principal`` or refuses.

    Args:
        tokens: The known tokens (or a holder whose table may change), or
            ``None`` for development mode (every request is ``DEV_PRINCIPAL``).

    In order: in development mode a ``Host`` that is not loopback is
    answered 403 ``{"error": "forbidden host"}``; a missing or unknown
    token is answered 401 ``{"error": "unauthorised"}`` with
    ``WWW-Authenticate: Bearer``; a viewer using a method other than GET
    or HEAD is answered 403 ``{"error": "forbidden"}``; a ``POST``
    without ``Content-Type: application/json`` is answered 415
    ``{"error": "use Content-Type: application/json"}``.

    """
    holder = tokens if isinstance(tokens, TokenHolder) else TokenHolder(tokens)

    async def check(request: HttpRequest, call_next: CallNext) -> Outcome:
        """Check the host, find the caller, check their role and the body type, and go on."""
        table = holder.table
        if table is None and not is_loopback_host(request.headers.get("Host")):
            return json({"error": "forbidden host"}, _FORBIDDEN)
        if request.path == HEALTH_PATH:
            return await call_next(request)
        if table is None:
            request.user = DEV_PRINCIPAL
            return await _typed(request, call_next)
        token = _bearer(request)
        found = table.lookup(token) if token is not None else None
        if found is None:
            return _unauthorised()
        principal = Principal(*found)
        if principal.role != "operator" and request.method not in READ_METHODS:
            return json({"error": "forbidden"}, _FORBIDDEN)
        request.user = principal
        return await _typed(request, call_next)

    return check


async def _typed(request: HttpRequest, call_next: CallNext) -> Outcome:
    """Refuse a ``POST`` whose body is not declared JSON (415); otherwise go on."""
    if request.method == "POST" and not _is_json(request):
        return json({"error": "use Content-Type: application/json"}, _UNSUPPORTED_MEDIA)
    return await call_next(request)
