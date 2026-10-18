"""Tests for the CLI's text helpers and the client's own checks."""

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from pyscheduler.cli_format import detail, duration, elapsed, table
from pyscheduler.client import ApiClient, ApiError

TEN_SECONDS = 10.0


class TestDuration:
    """``duration`` writes seconds the short way."""

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(None, "-"), (0.42, "0.4s"), (12.0, "12s"), (185.0, "3m05s"), (3720.0, "1h02m")],
    )
    def test_duration(self, seconds: float | None, text: str) -> None:
        """Pick the unit by size."""
        assert duration(seconds) == text

    def test_elapsed(self) -> None:
        """Count the seconds since an ISO instant; nothing for no instant."""
        now = datetime(2026, 10, 19, 9, 0, 10, tzinfo=UTC)
        assert elapsed("2026-10-19T09:00:00Z", now) == TEN_SECONDS
        assert elapsed(None, now) is None


class TestTable:
    """``table`` lines up columns."""

    def test_aligned(self) -> None:
        """Pad every column to its widest cell, showing ``-`` for empty cells."""
        assert table(("A", "BB"), [["xyz", None], ["q", 1]]).splitlines() == [
            "A    BB",
            "xyz  -",
            "q    1",
        ]

    def test_detail_without_attempts(self) -> None:
        """A run with no attempts or events says so."""
        text = detail({"run": {"run_id": "a.2026-10-19.1", "state": "scheduled"}})
        assert text.count("(none)") == 2  # noqa: PLR2004 -- attempts and events


class TestClientChecks:
    """``ApiClient`` refuses what it cannot send."""

    def test_bad_scheme(self) -> None:
        """Only http and https URLs are used."""
        with pytest.raises(ApiError, match="http://"):
            ApiClient("file:///etc", None).health()

    def test_unknown_action(self) -> None:
        """An action the API does not have is refused before any request."""
        with pytest.raises(ValueError, match="unknown run action"):
            ApiClient("http://127.0.0.1:1", None).run_action("a.2026-10-19.1", "explode")


class TestModule:
    """``python -m pyscheduler`` runs the CLI."""

    def test_help(self) -> None:
        """``--help`` lists the commands and exits 0."""
        done = subprocess.run(
            [sys.executable, "-m", "pyscheduler", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0
        assert "hash-token" in done.stdout
