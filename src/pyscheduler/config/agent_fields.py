"""Read the ``agents:`` section and the agents' secrets file.

The secrets file is the club's membership book: one line per agent with the
password only that agent and the scheduler know. The ``agents:`` section
says where the club meets (a port to listen on, or an outside broker) and
how patient to be with members who go quiet.
"""

import stat
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from pyscheduler.config.fields import Reader, check_keys, read_duration, read_map, read_str
from pyscheduler.config.model import AgentsDef, SourceRef
from pyscheduler.config.settings_fields import read_listen
from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, Scalar, Seq, YamlError, parse

__all__ = [
    "AGENTS_KEYS",
    "DEFAULT_AGENT_GRACE",
    "DEFAULT_LOST_AFTER",
    "MIN_SECRET_LENGTH",
    "AgentSecrets",
    "AgentSecretsError",
    "read_agents_section",
]

AGENTS_KEYS = ("listen", "broker", "secrets_file", "agent_grace", "lost_after")
DEFAULT_AGENT_GRACE = timedelta(seconds=60)
DEFAULT_LOST_AFTER = timedelta(minutes=10)
MIN_SECRET_LENGTH = 32
_FIELDS = ("name", "secret")
_OTHERS_MASK = stat.S_IRWXG | stat.S_IRWXO


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
            raise AgentSecretsError(filename, 0, str(exc)) from None
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


def _check_file(r: Reader, items: Mapping[str, Node], path: Path) -> None:
    """Report an unreadable, malformed or too-open secrets file."""
    line = items["secrets_file"].line
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = 0
    if mode & _OTHERS_MASK:
        r.add(line, f"{path} can be read by other users: run chmod 600 on it", "warning")
    try:
        AgentSecrets.load(path)
    except AgentSecretsError as exc:
        # Point at the secrets file itself when the fault is in it.
        if exc.line:
            r.for_file(exc.file).add(exc.line, exc.reason)
        else:
            r.add(line, exc.reason)


def read_agents_section(r: Reader, main: Mapping[str, Node]) -> AgentsDef | None:
    """Read ``agents:`` from the main file's top level; ``None`` if it is absent or broken."""
    if "agents" not in main:
        return None
    node = read_map(r, main["agents"], "agents")
    if node is None:
        return None
    check_keys(r, node, AGENTS_KEYS, "agents")
    items = node.items
    listen = read_listen(r, items["listen"], "listen") if "listen" in items else None
    broker = read_listen(r, items["broker"], "broker") if "broker" in items else None
    if listen is not None and broker is not None:
        r.add(node.line, "agents: use either listen or broker, not both")
    if "secrets_file" not in items:
        r.add(node.line, "agents needs secrets_file")
        return None
    text = read_str(r, items["secrets_file"], "secrets_file")
    if text is None:
        return None
    secrets_file = Path(r.file).absolute().parent / text
    _check_file(r, items, secrets_file)
    return AgentsDef(
        listen=listen,
        broker=broker,
        secrets_file=secrets_file,
        agent_grace=_duration(r, items, "agent_grace", DEFAULT_AGENT_GRACE),
        lost_after=_duration(r, items, "lost_after", DEFAULT_LOST_AFTER),
        source=SourceRef(r.file, node.line),
    )


def _duration(r: Reader, items: Mapping[str, Node], key: str, default: timedelta) -> timedelta:
    """Read an optional duration with a default."""
    value = read_duration(r, items[key], key) if key in items else None
    return default if value is None else value
