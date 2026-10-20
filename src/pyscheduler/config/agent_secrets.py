"""Read the agents' shared-secrets file.

The membership book of the club: one line per agent with the password only
that agent and the scheduler know.
"""

from collections.abc import Mapping
from pathlib import Path

from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, Scalar, Seq, YamlError, parse

__all__ = ["MIN_SECRET_LENGTH", "AgentSecrets", "AgentSecretsError"]

MIN_SECRET_LENGTH = 32
_FIELDS = ("name", "secret")


class AgentSecretsError(ValueError):
    """Raised when the secrets file cannot be read or is malformed.

    ``file`` and ``line`` say where; the message never holds a secret.
    """

    def __init__(self, file: str, line: int, message: str) -> None:
        """Remember the place and build the ``file:line: message`` text."""
        super().__init__(f"{file}:{line}: {message}" if line else f"{file}: {message}")
        self.file = file
        self.line = line
        self.reason = message


class AgentSecrets:
    """Hold each agent's shared secret; use ``load`` to read a file."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        """Keep a copy of the ``name -> secret`` mapping."""
        self._secrets = dict(secrets)

    def __repr__(self) -> str:
        """Show the agent names only, never a secret."""
        return f"AgentSecrets(names={sorted(self._secrets)})"

    @property
    def names(self) -> frozenset[str]:
        """Return the known agent names."""
        return frozenset(self._secrets)

    def get(self, name: str) -> str | None:
        """Return the secret of agent ``name``, or ``None`` if it is unknown."""
        return self._secrets.get(name)

    @classmethod
    def load(cls, path: Path) -> AgentSecrets:
        """Read the secrets file at ``path``: a list of ``{name, secret}``.

        Raises:
            AgentSecretsError: If the file cannot be read or is malformed,
                a name is bad or repeated, or a secret is under 32
                characters. The error names the file and line.

        """
        filename = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentSecretsError(filename, 0, f"cannot read secrets file: {exc}") from None
        try:
            root = parse(text, filename)
        except YamlError as exc:
            raise AgentSecretsError(exc.filename, exc.line, exc.message) from None
        if not isinstance(root, Seq):
            raise AgentSecretsError(filename, root.line, "must be a list of {name, secret}")
        found: dict[str, str] = {}
        for item in root.items:
            name, secret = _entry(item, filename)
            if name in found:
                raise AgentSecretsError(filename, item.line, f"agent {name!r} is listed twice")
            found[name] = secret
        return cls(found)


def _text(item: Map, filename: str, key: str) -> str:
    """Return the scalar ``key`` of ``item`` or raise."""
    node = item.items.get(key)
    if node is None:
        raise AgentSecretsError(filename, item.line, f"missing {key!r}")
    if not isinstance(node, Scalar):
        raise AgentSecretsError(filename, node.line, f"{key!r} must be text")
    return node.value


def _entry(item: Node, filename: str) -> tuple[str, str]:
    """Check one list item and return ``(name, secret)``."""
    if not isinstance(item, Map):
        raise AgentSecretsError(filename, item.line, "each agent must be a mapping of name, secret")
    for key in item.items:
        if key not in _FIELDS:
            raise AgentSecretsError(filename, item.key_lines[key], f"unknown field {key!r}")
    name = _text(item, filename, "name")
    try:
        check_name(name)
    except ValueError as exc:
        raise AgentSecretsError(filename, item.line, f"bad name: {exc}") from None
    secret = _text(item, filename, "secret")
    if len(secret) < MIN_SECRET_LENGTH:
        line = item.items["secret"].line
        msg = f"the secret for {name!r} must be at least {MIN_SECRET_LENGTH} characters"
        raise AgentSecretsError(filename, line, msg)
    return name, secret
