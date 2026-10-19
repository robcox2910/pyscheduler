"""Check that every job's agent settings point at something real.

Like checking that a delivery slip names a courier who works for us: the
agent must be in the secrets file, its tags must be well formed, and a job
cannot ask for one named courier and for any courier with a skill.
"""

from typing import Protocol

from pyscheduler.config.agent_fields import AgentSecrets, AgentSecretsError
from pyscheduler.config.model import AgentsDef, Config, JobDef, SourceRef
from pyscheduler.values import check_name


class Checker(Protocol):
    """What the agent checks need from the validator: a place to note problems."""

    broken: frozenset[str]

    def add(self, where: SourceRef, message: str, job: str | None) -> None:
        """Note one error at ``where``."""
        ...


def check_agents(checker: Checker, config: Config) -> None:
    """Note agent jobs that name unknown agents, bad tags, both settings, or no ``agents:``."""
    agents = config.agents
    known: frozenset[str] | None = None
    if agents is not None:
        try:
            known = AgentSecrets.load(agents.secrets_file).names
        except AgentSecretsError:
            known = None  # the loader already reported the unreadable file
    for job in config.jobs.values():
        if job.name in checker.broken or (job.agent is None and not job.agent_tags):
            continue
        for message in _problems(job, agents, known):
            checker.add(job.source, message, job.name)


def _problems(job: JobDef, agents: AgentsDef | None, known: frozenset[str] | None) -> list[str]:
    """Return what is wrong with one agent job's settings."""
    found: list[str] = []
    if job.agent is not None and job.agent_tags:
        found.append("use either agent or agent_tags, not both")
    if agents is None:
        found.append("this job needs an agent, but there is no agents: section")
    elif agents.listen is None and agents.broker is None:
        found.append("this job needs an agent, but agents: sets neither listen nor broker")
    if job.agent is not None:
        found.extend(_name_problems("agent", job.agent))
        if known is not None and job.agent not in known:
            found.append(f"unknown agent {job.agent!r}: it is not in the agents secrets file")
    for tag in job.agent_tags:
        found.extend(_name_problems("agent tag", tag))
    return found


def _name_problems(what: str, value: str) -> list[str]:
    """Return a message if ``value`` breaks the job-name rule."""
    try:
        check_name(value)
    except ValueError as exc:
        return [f"{what}: {exc}"]
    return []


__all__ = ["check_agents"]
