"""The users file: the people who may log in to the web UI, and their roles.

Think of a members' club. The doorman keeps a list of members and the
colour of each one's card: green (a **viewer**) may look around, gold
(an **operator**) may also work the machines, and black (an **admin**)
may also change the house rules. The list never holds anyone's
password, only a *fingerprint* of it (a slow PBKDF2 hash), so a stolen
list lets nobody in.

The users file is a YAML list::

    - {name: ada, password_hash: pbkdf2_sha256$100000$..., role: admin}
    - {name: vic, password_hash: pbkdf2_sha256$100000$..., role: viewer}

``pysched hash-password`` prints the ``password_hash`` line for a new
password. A reload re-reads the file (``UserHolder.table``), so a
removed user's browser session stops working at its next request.
"""

from dataclasses import dataclass
from pathlib import Path

from pyweb.auth import User, UserStore

from pyscheduler.api.auth import ROLE_RANK
from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, Scalar, Seq, YamlError, parse

__all__ = ["UserHolder", "UserTable", "UsersFileError"]

_FIELDS = ("name", "password_hash", "role")
_SCHEME = "pbkdf2_sha256"
_HASH_PARTS = 4
_HEX_DIGITS = frozenset("0123456789abcdef")


class UsersFileError(ValueError):
    """Raised when the users file cannot be read or is malformed."""


def _text(node: Node | None, where: str, field: str) -> str:
    """Return the scalar text of ``field``, or raise ``UsersFileError``."""
    if node is None:
        msg = f"{where}: missing {field!r}"
        raise UsersFileError(msg)
    if not isinstance(node, Scalar):
        msg = f"{where}: {field!r} must be text"
        raise UsersFileError(msg)
    return node.value


def _is_hex(text: str) -> bool:
    """Return whether ``text`` is a non-empty run of lowercase hex digits."""
    return bool(text) and set(text) <= _HEX_DIGITS


def _good_hash(text: str) -> bool:
    """Return whether ``text`` looks like ``pbkdf2_sha256$<rounds>$<salt hex>$<key hex>``."""
    parts = text.split("$")
    if len(parts) != _HASH_PARTS:
        return False
    scheme, rounds, salt, key = parts
    return (
        scheme == _SCHEME
        and rounds.isascii()
        and rounds.isdigit()
        and int(rounds) >= 1
        and _is_hex(salt)
        and _is_hex(key)
    )


def _user(item: Node, filename: str) -> User:
    """Check one list item and turn it into a pyweb ``User`` with one role."""
    where = f"{filename}:{item.line}"
    if not isinstance(item, Map):
        msg = f"{where}: each user must be a mapping of {', '.join(_FIELDS)}"
        raise UsersFileError(msg)
    unknown = sorted(set(item.items) - set(_FIELDS))
    if unknown:
        msg = f"{where}: unknown field {unknown[0]!r}"
        raise UsersFileError(msg)
    name = _text(item.items.get("name"), where, "name")
    try:
        check_name(name)
    except ValueError as exc:
        msg = f"{where}: bad name: {exc}"
        raise UsersFileError(msg) from None
    digest = _text(item.items.get("password_hash"), where, "password_hash")
    if not _good_hash(digest):
        msg = (
            f"{where}: password_hash must be a pbkdf2_sha256$... hash "
            "(make one with pysched hash-password)"
        )
        raise UsersFileError(msg)
    role = _text(item.items.get("role"), where, "role")
    if role not in ROLE_RANK:
        msg = f"{where}: role must be viewer, operator or admin, not {role!r}"
        raise UsersFileError(msg)
    return User(name, digest, frozenset({role}))


class UserTable(UserStore):
    """Hold the users of a users file and look them up by name."""

    def __init__(self, users: tuple[User, ...]) -> None:
        """Keep the checked users; use ``load`` to read a file."""
        self._users = {user.name: user for user in users}

    @classmethod
    def load(cls, path: Path) -> UserTable:
        """Read the users file at ``path``.

        Raises:
            UsersFileError: If the file cannot be read, is not a list of
                ``{name, password_hash, role}`` mappings, has an unknown
                role or a malformed hash, or repeats a name. The message
                names the file and line.

        """
        filename = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"{filename}: cannot read users file: {exc}"
            raise UsersFileError(msg) from None
        try:
            root = parse(text, filename)
        except YamlError as exc:
            raise UsersFileError(str(exc)) from None
        if not isinstance(root, Seq):
            msg = f"{filename}:{root.line}: the users file must be a list of users"
            raise UsersFileError(msg)
        users: list[User] = []
        seen: set[str] = set()
        for item in root.items:
            user = _user(item, filename)
            if user.name in seen:
                msg = f"{filename}:{item.line}: the user {user.name!r} is listed twice"
                raise UsersFileError(msg)
            seen.add(user.name)
            users.append(user)
        return cls(tuple(users))

    def get(self, name: str) -> User | None:
        """Return the user called ``name``, or ``None``."""
        return self._users.get(name)


@dataclass
class UserHolder(UserStore):
    """Hold the current users table (``None`` means there is no users file).

    The session middleware asks the holder for each request's user, so
    replacing ``table`` (a reload re-reading the users file) takes effect
    at once.
    """

    table: UserTable | None

    def get(self, name: str) -> User | None:
        """Return the user called ``name`` in the current table, or ``None``."""
        return None if self.table is None else self.table.get(name)
