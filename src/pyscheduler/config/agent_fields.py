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

from pyscheduler.config.agent_secrets import MIN_SECRET_LENGTH, AgentSecrets, AgentSecretsError
from pyscheduler.config.agents_model import AgentsDef
from pyscheduler.config.fields import Reader, check_keys, read_duration, read_map, read_str
from pyscheduler.config.settings_fields import read_listen
from pyscheduler.config.source_ref import SourceRef
from pyscheduler.yamlish import Node

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
_OTHERS_MASK = stat.S_IRGRP | stat.S_IROTH


def _load_secrets(r: Reader, items: Mapping[str, Node], path: Path) -> AgentSecrets | None:
    """Warn if the secrets file is readable by others, then load it; note problems."""
    line = items["secrets_file"].line
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = 0
    if mode & _OTHERS_MASK:
        r.add(line, f"{path} can be read by other users: run chmod 600 on it", "warning")
    try:
        return AgentSecrets.load(path)
    except AgentSecretsError as exc:
        # Point at the secrets file itself when the fault is in it.
        if exc.line:
            r.for_file(exc.file).add(exc.line, exc.reason)
        else:
            r.add(line, exc.reason)
    return None


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
    secrets = _load_secrets(r, items, secrets_file)
    if secrets is None:
        return None
    return AgentsDef(
        listen=listen,
        broker=broker,
        secrets_file=secrets_file,
        agent_grace=_duration(r, items, "agent_grace", DEFAULT_AGENT_GRACE),
        lost_after=_duration(r, items, "lost_after", DEFAULT_LOST_AFTER),
        source=SourceRef(r.file, node.line),
        secrets=secrets,
    )


def _duration(r: Reader, items: Mapping[str, Node], key: str, default: timedelta) -> timedelta:
    """Read an optional duration with a default."""
    value = read_duration(r, items[key], key) if key in items else None
    return default if value is None else value
