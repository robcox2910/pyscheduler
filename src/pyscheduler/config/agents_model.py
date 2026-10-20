"""The model of the ``agents:`` section (re-exported by ``pyscheduler.config.model``).

Like the page of the recipe book that says where the delivery vans
meet the kitchen, and how long to wait for a van before giving up on it.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.config.source_ref import SourceRef

__all__ = ["AgentsDef"]


@dataclass(frozen=True)
class AgentsDef:
    """The ``agents:`` section: where agents meet the scheduler and how long to wait for them.

    ``listen`` embeds a broker in the scheduler; ``broker`` uses an external
    one. At most one is set; with neither, agents are off. The secrets file
    holds each agent's shared secret and is read separately.
    """

    listen: tuple[str, int] | None
    broker: tuple[str, int] | None
    secrets_file: Path
    agent_grace: timedelta
    lost_after: timedelta
    source: SourceRef
    secrets: AgentSecrets = field(
        default_factory=lambda: AgentSecrets({}), repr=False, compare=False
    )
