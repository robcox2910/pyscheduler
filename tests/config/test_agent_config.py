"""Tests for the ``agents:`` config section and the agents secrets file."""

import textwrap
from pathlib import Path

import pytest

from pyscheduler.config import Config, ConfigError, Problem, load_config
from pyscheduler.config.agent_fields import AgentSecrets, AgentSecretsError

SECRET = "x" * 40
SHORT = "short-secret-value"
DEFAULT_GRACE_SECONDS = 60
DEFAULT_LOST_SECONDS = 600
PORT = 8471
TWO_MINUTES = 120
SECOND_LINE = 2
OPEN_MODE = 0o640
CLOSED_MODE = 0o600

SECRETS = f"- {{name: build-01, secret: {SECRET}}}\n- {{name: gpu-02, secret: {SECRET}y}}\n"
JOB = """\
jobs:
  nightly:
    command: "true"
    schedule: manual
"""
AGENTS = 'agents: {listen: "127.0.0.1:8471", secrets_file: agents.yaml}\n'


def _setup(tmp_path: Path, config: str, secrets: str = SECRETS, mode: int = CLOSED_MODE) -> Path:
    """Write the secrets file and ``scheduler.yaml``; return the config path."""
    secrets_path = tmp_path / "agents.yaml"
    secrets_path.write_text(secrets, encoding="utf-8")
    secrets_path.chmod(mode)
    path = tmp_path / "scheduler.yaml"
    path.write_text(textwrap.dedent(config), encoding="utf-8")
    return path


def _load(tmp_path: Path, config: str, secrets: str = SECRETS, mode: int = CLOSED_MODE) -> Config:
    """Load a config that must be valid."""
    return load_config(_setup(tmp_path, config, secrets, mode), env={})


def _problems(tmp_path: Path, config: str, secrets: str = SECRETS) -> tuple[Problem, ...]:
    """Return the problems of a config that must be invalid."""
    with pytest.raises(ConfigError) as info:
        load_config(_setup(tmp_path, config, secrets), env={})
    return info.value.problems


def _job(extra: str) -> str:
    """Return an agents section and a job with ``extra`` lines."""
    return AGENTS + JOB + textwrap.indent(extra, "    ")


class TestSection:
    """The ``agents:`` section loads with defaults and checks its keys."""

    def test_loads_with_defaults(self, tmp_path: Path) -> None:
        """Read listen and secrets_file, leaving grace and lost_after at their defaults."""
        agents = _load(tmp_path, AGENTS + JOB).agents
        assert agents is not None
        assert agents.listen == ("127.0.0.1", PORT)
        assert agents.broker is None
        assert agents.secrets_file == tmp_path / "agents.yaml"
        assert agents.agent_grace.total_seconds() == DEFAULT_GRACE_SECONDS
        assert agents.lost_after.total_seconds() == DEFAULT_LOST_SECONDS
        assert agents.source.line == 1

    def test_absent_is_none(self, tmp_path: Path) -> None:
        """Leave ``Config.agents`` as None without the section."""
        assert _load(tmp_path, JOB).agents is None

    def test_durations_and_broker(self, tmp_path: Path) -> None:
        """Read an external broker and the two durations."""
        text = "agents: {broker: 'mq:5672', secrets_file: agents.yaml, agent_grace: 30s, lost_after: 2m}\n"
        agents = _load(tmp_path, text + JOB).agents
        assert agents is not None
        assert agents.broker == ("mq", 5672)
        assert agents.listen is None
        assert agents.lost_after.total_seconds() == TWO_MINUTES

    def test_listen_and_broker_together(self, tmp_path: Path) -> None:
        """Refuse both listen and broker."""
        text = "agents: {listen: 'a:1', broker: 'b:2', secrets_file: agents.yaml}\n" + JOB
        [problem] = _problems(tmp_path, text)
        assert "either listen or broker" in problem.message
        assert problem.line == 1

    def test_needs_secrets_file(self, tmp_path: Path) -> None:
        """Refuse a section with no secrets_file."""
        [problem] = _problems(tmp_path, "agents: {listen: 'a:1'}\n" + JOB)
        assert "secrets_file" in problem.message

    def test_unknown_key(self, tmp_path: Path) -> None:
        """Refuse an unknown key, suggesting the close one."""
        text = "agents: {listen: 'a:1', secrets_file: agents.yaml, agent_grac: 1s}\n" + JOB
        [problem] = _problems(tmp_path, text)
        assert "agent_grace" in problem.message


class TestJobs:
    """Agent jobs are checked against the section and the secrets file."""

    def test_known_agent_and_tags(self, tmp_path: Path) -> None:
        """Accept a known agent, and tags alone."""
        assert _load(tmp_path, _job("agent: build-01\n")).jobs["nightly"].agent == "build-01"
        tagged = _load(tmp_path, _job("agent_tags: [linux, gpu]\n"))
        assert tagged.jobs["nightly"].agent_tags == ("linux", "gpu")

    def test_unknown_agent(self, tmp_path: Path) -> None:
        """Refuse an agent that is not in the secrets file."""
        [problem] = _problems(tmp_path, _job("agent: ghost\n"))
        assert "unknown agent 'ghost'" in problem.message
        assert problem.job == "nightly"

    def test_bad_tag(self, tmp_path: Path) -> None:
        """Refuse a tag that breaks the name rule."""
        [problem] = _problems(tmp_path, _job("agent_tags: ['bad tag!']\n"))
        assert "agent tag" in problem.message

    def test_agent_and_tags(self, tmp_path: Path) -> None:
        """Refuse a job with both agent and agent_tags."""
        [problem] = _problems(tmp_path, _job("agent: build-01\nagent_tags: [linux]\n"))
        assert "not both" in problem.message

    def test_no_section(self, tmp_path: Path) -> None:
        """Refuse an agent job when there is no agents section."""
        [problem] = _problems(tmp_path, JOB + "    agent: build-01\n")
        assert "no agents: section" in problem.message

    def test_section_without_listen_or_broker(self, tmp_path: Path) -> None:
        """Refuse an agent job when agents are off."""
        text = "agents: {secrets_file: agents.yaml}\n" + JOB + "    agent: build-01\n"
        [problem] = _problems(tmp_path, text)
        assert "neither listen nor broker" in problem.message


class TestSecretsFile:
    """The secrets file: format, length, permissions and secrecy."""

    def test_short_secret_at_file_line(self, tmp_path: Path) -> None:
        """Name the secrets file and the line of a too-short secret."""
        secrets = f"- {{name: build-01, secret: {SECRET}}}\n- {{name: gpu-02, secret: {SHORT}}}\n"
        problems = _problems(tmp_path, AGENTS + JOB, secrets)
        assert len(problems) >= 1
        problem = problems[0]
        assert problem.file.endswith("agents.yaml")
        assert problem.line == SECOND_LINE
        assert "at least 32" in problem.message

    def test_secret_never_in_problems(self, tmp_path: Path) -> None:
        """Keep secret values out of every problem message."""
        secrets = f"- {{name: build-01, secret: {SHORT}}}\n"
        problems = _problems(tmp_path, AGENTS + JOB, secrets)
        assert problems
        assert all(SHORT not in str(p) for p in problems)
        bad = _problems(tmp_path, _job("agent: ghost\n"), f"- {{name: ok, secret: {SECRET}}}\n")
        assert all(SECRET not in str(p) for p in bad)

    def test_group_readable_warns(self, tmp_path: Path) -> None:
        """Warn about a secrets file other users can read."""
        config = _load(tmp_path, AGENTS + JOB, mode=OPEN_MODE)
        [warning] = config.warnings
        assert warning.severity == "warning"
        assert "chmod 600" in warning.message
        assert SECRET not in str(warning)

    def test_group_writable_only_is_quiet(self, tmp_path: Path) -> None:
        """Warn only about read bits, not a write-only group bit."""
        assert _load(tmp_path, AGENTS + JOB, mode=0o620).warnings == ()

    def test_yaml_syntax_error_at_secrets_line(self, tmp_path: Path) -> None:
        """Report a YAML error at the secrets file's own line, once."""
        problems = _problems(tmp_path, AGENTS + JOB, "- {name: a, secret: [\n")
        assert problems
        assert problems[0].file.endswith("agents.yaml")
        assert problems[0].line >= 1
        assert str(tmp_path) not in problems[0].message

    def test_broken_section_not_reported_per_job(self, tmp_path: Path) -> None:
        """Do not add 'no agents: section' for jobs when the section itself is broken."""
        text = "agents: {listen: 'a:1'}\n" + JOB + "    agent: build-01\n"
        problems = _problems(tmp_path, text)
        assert [p.message for p in problems] == ["agents needs secrets_file"]

    def test_secrets_carried_on_section(self, tmp_path: Path) -> None:
        """Carry the loaded secrets on the section without showing them in its repr."""
        agents = _load(tmp_path, AGENTS + JOB).agents
        assert agents is not None
        assert agents.secrets.get("build-01") == SECRET
        assert SECRET not in repr(agents)

    def test_private_file_has_no_warning(self, tmp_path: Path) -> None:
        """Say nothing about a mode 600 file."""
        assert _load(tmp_path, AGENTS + JOB).warnings == ()

    def test_missing_file(self, tmp_path: Path) -> None:
        """Report an unreadable secrets file at the secrets_file key."""
        path = _setup(tmp_path, AGENTS + JOB)
        (tmp_path / "agents.yaml").unlink()
        with pytest.raises(ConfigError) as info:
            load_config(path, env={})
        [problem] = info.value.problems
        assert "cannot read" in problem.message
        assert problem.file.endswith("scheduler.yaml")

    @pytest.mark.parametrize(
        ("text", "phrase", "line"),
        [
            (f"- {{name: 'bad name', secret: {SECRET}}}\n", "bad name", 1),
            (
                f"- {{name: a, secret: {SECRET}}}\n- {{name: a, secret: {SECRET}}}\n",
                "listed twice",
                2,
            ),
            ("- {name: a}\n", "missing 'secret'", 1),
            (f"- {{name: a, secret: {SECRET}, extra: 1}}\n", "unknown field 'extra'", 1),
            ("name: a\n", "must be a list", 1),
            ("- just-text\n", "must be a mapping", 1),
        ],
    )
    def test_load_errors(self, tmp_path: Path, text: str, phrase: str, line: int) -> None:
        """Raise ``AgentSecretsError`` naming file and line."""
        path = tmp_path / "agents.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(AgentSecretsError) as info:
            AgentSecrets.load(path)
        assert phrase in str(info.value)
        assert info.value.line == line
        assert str(path) in str(info.value)

    def test_load_and_repr(self, tmp_path: Path) -> None:
        """Look secrets up by name and keep them out of the repr."""
        path = tmp_path / "agents.yaml"
        path.write_text(SECRETS, encoding="utf-8")
        secrets = AgentSecrets.load(path)
        assert secrets.get("build-01") == SECRET
        assert secrets.get("ghost") is None
        assert secrets.names == {"build-01", "gpu-02"}
        assert SECRET not in repr(secrets)
