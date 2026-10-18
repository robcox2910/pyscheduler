"""``${ENV}`` values with braces, and secrets kept out of problem messages (R28, R29)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from pyscheduler.api.serialize import problem_json
from pyscheduler.config import ConfigError, Problem, load_config
from pyscheduler.config.redaction import redact_text
from pyscheduler.templates import render

type WriteYaml = Callable[[str, str], Path]

BRACED = "pa{ss}w0rd"
VALUES = {"job": "a", "run_date": "2026-10-19", "log_dir": "/logs", "run_id": "a.1"}


class TestBracesInTemplates:
    """An expanded value renders back to itself, braces and all."""

    def test_template_fields_render_literally(self, write_yaml: WriteYaml) -> None:
        """Command (string and list), cwd, env values, stdout and stderr keep the braces."""
        path = write_yaml(
            "pysched.yaml",
            """
            jobs:
              a:
                command: login --pw ${PW}
                schedule: manual
                cwd: /srv/${PW}
                env: {PASSWORD: "${PW}"}
                stdout: /logs/${PW}.out
                stderr: /logs/${PW}.err
              b:
                command: [login, "${PW}"]
                schedule: manual
                description: uses ${PW}
            """,
        )
        config = load_config(path, env={"PW": BRACED})
        a, b = config.jobs["a"], config.jobs["b"]
        assert isinstance(a.command, str)
        assert render(a.command, VALUES) == f"login --pw {BRACED}"
        assert a.cwd is not None
        assert render(a.cwd, VALUES) == f"/srv/{BRACED}"
        assert render(a.env["PASSWORD"], VALUES) == BRACED
        assert render(a.stdout, VALUES) == f"/logs/{BRACED}.out"
        assert render(a.stderr, VALUES) == f"/logs/{BRACED}.err"
        assert [render(word, VALUES) for word in b.command] == ["login", BRACED]
        assert b.description == f"uses {BRACED}"  # not a template: as it is
        assert dict(config.expansions) == {BRACED: "PW"}

    def test_placeholder_like_value_stays_literal(self, write_yaml: WriteYaml) -> None:
        """A value that looks like ``{job}`` is not filled in as a placeholder."""
        path = write_yaml("pysched.yaml", "jobs:\n  a: {command: 'echo ${V}', schedule: manual}\n")
        config = load_config(path, env={"V": "{job}"})
        command = config.jobs["a"].command
        assert isinstance(command, str)
        assert render(command, VALUES) == "echo {job}"


class TestSecretsInProblems:
    """No problem message shows an expanded value."""

    def test_converter_error_is_redacted(self, write_yaml: WriteYaml) -> None:
        """``invalid duration '<secret>'`` says ``${T}`` instead."""
        path = write_yaml(
            "pysched.yaml",
            "jobs:\n  a: {command: run, schedule: manual, timeout: '${T}'}\n",
        )
        with pytest.raises(ConfigError) as caught:
            load_config(path, env={"T": "hunter22"})
        messages = [p.message for p in caught.value.problems]
        assert all("hunter22" not in m for m in messages)
        assert any("${T}" in m for m in messages)
        assert "hunter22" not in str(caught.value)

    def test_validation_error_is_redacted(self, write_yaml: WriteYaml) -> None:
        """A validation message quoting a calendar name from the environment hides it."""
        path = write_yaml(
            "pysched.yaml",
            "jobs:\n  a: {command: run, schedule: manual, calendar: '${CAL}'}\n",
        )
        with pytest.raises(ConfigError) as caught:
            load_config(path, env={"CAL": "secret-calendar"})
        messages = [p.message for p in caught.value.problems]
        assert all("secret-calendar" not in m for m in messages), messages
        assert any("${CAL}" in m for m in messages), messages


class TestRedactText:
    """The shared redaction: longest first, short values kept, idempotent."""

    def test_rules(self) -> None:
        """Hide 4+ character values (longest first), and the brace-doubled form too."""
        expansions = {"abc": "SHORT", "hunter2": "PW", "hunter22": "PW2", "a{b}cd": "BR"}
        text = "x hunter22 hunter2 abc a{{b}}cd a{b}cd"
        once = redact_text(text, expansions)
        assert once == "x ${PW2} ${PW} abc ${BR} ${BR}"
        assert redact_text(once, {**expansions, "PW2}": "ODD"}) == once

    def test_problem_json_redacts(self) -> None:
        """``problem_json`` hides values when it is given the expansions."""
        problem = Problem("f.yaml", 3, "bad value 'hunter22'")
        assert problem_json(problem, {"hunter22": "PW"})["message"] == "bad value '${PW}'"
        assert problem_json(problem)["message"] == "bad value 'hunter22'"
