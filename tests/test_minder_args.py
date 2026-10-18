"""Tests for the minder's command line and for how little it imports (it starts once per attempt)."""

import json
import subprocess
import sys
from typing import Any

import pytest

from pyscheduler.minder import dumps, parse_args

USAGE_EXIT = 2
"""The exit code of a bad minder command line (as argparse would give)."""

HEAVY = (
    "argparse",
    "contextlib",
    "dataclasses",
    "inspect",
    "json",
    "pathlib",
    "re",
    "subprocess",
    "typing",
)
"""Modules the minder must not import: each costs milliseconds on every attempt's start."""


class TestParseArgs:
    """``parse_args`` reads ``--status``, ``--stdout``, ``--stderr`` and the command after ``--``."""

    def test_all_options(self) -> None:
        """Every option and the command are read."""
        args = parse_args(["--status", "s", "--stdout", "o", "--stderr", "e", "--", "echo", "hi"])
        assert args.status == "s"
        assert args.stdout == "o"
        assert args.stderr == "e"
        assert args.command == ["echo", "hi"]

    def test_stderr_is_optional(self) -> None:
        """Without ``--stderr`` it is None; option order does not matter."""
        args = parse_args(["--stdout", "o", "--status", "s", "--", "true"])
        assert args.stderr is None
        assert args.command == ["true"]

    def test_command_words_may_look_like_options(self) -> None:
        """Words after ``--`` belong to the command, even ``--status``."""
        args = parse_args(["--status", "s", "--stdout", "o", "--", "ls", "--status"])
        assert args.command == ["ls", "--status"]

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--stdout", "o", "--", "true"], id="no-status"),
            pytest.param(["--status", "s", "--", "true"], id="no-stdout"),
            pytest.param(["--status", "s", "--stdout", "o", "--"], id="no-command"),
            pytest.param(["--status", "s", "--stdout", "o", "true"], id="no-dashes"),
            pytest.param(["--status", "s", "--stdout", "o", "--bogus", "x", "--", "t"], id="bogus"),
            pytest.param(
                ["--status", "s", "--status", "t", "--stdout", "o", "--", "t"], id="twice"
            ),
            pytest.param(["--status"], id="missing-value"),
        ],
    )
    def test_bad_command_lines_exit_two(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad command line prints the usage to stderr and exits 2."""
        with pytest.raises(SystemExit) as caught:
            parse_args(argv)
        assert caught.value.code == USAGE_EXIT
        assert "usage:" in capsys.readouterr().err


class TestLightImports:
    """The minder starts once per attempt, so it imports only what it needs."""

    def test_no_heavy_modules(self) -> None:
        """Importing the minder pulls in none of the slow modules."""
        probe = (
            "import sys, pyscheduler.minder; "
            f"print(','.join(m for m in {HEAVY!r} if m in sys.modules))"
        )
        found = subprocess.run(  # noqa: S603 -- our own interpreter and probe
            [sys.executable, "-I", "-c", probe], check=True, capture_output=True, text=True
        ).stdout.strip()
        assert found == ""


class TestDumps:
    """``dumps`` writes the same JSON as ``json.dumps`` for the values a note holds."""

    @pytest.mark.parametrize(
        "value",
        [
            "plain",
            'quote " and back \\ slash',
            "new\nline\ttab\rreturn\x00nul\x1fus\x7fdel\bback\fform",
            "caf\u00e9 \u65e5\u672c \U0001f600",
            "lone \udcff surrogate",
            0,
            -3,
            12345678901234567890,
            True,
            False,
            None,
        ],
    )
    def test_matches_json(self, value: str | int | bool | None) -> None:
        """Each value comes out exactly as ``json.dumps`` writes it, and reads back the same."""
        note = {"key \u00e9": value, "pid": 1}
        assert dumps(note) == json.dumps(note)
        assert json.loads(dumps(note)) == note

    def test_other_values_are_refused(self) -> None:
        """A float (or anything else) is not a note value."""
        bad: Any = {"x": 1.5}
        with pytest.raises(TypeError):
            dumps(bad)
