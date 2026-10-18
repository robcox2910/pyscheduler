"""Tests for the ``pysched`` command line: offline commands and commands against a server."""

import hashlib
from pathlib import Path

import pytest

from pyscheduler.cli import main
from pyscheduler.client import ApiClient
from service_helpers import today, wait_for_states, with_service, write_config

OK = 0
SERVER_ERROR = 1
USAGE = 2
TOKEN_BYTES = 32
SHA256_HEX = 64

FORECAST_JOBS = """
jobs:
  extract:
    command: run-extract
    schedule: "30 18 * * *"
  load:
    command: run-load
    depends_on: [extract]
"""


class TestValidate:
    """``pysched validate FILE`` checks a config offline."""

    def test_validate_ok_and_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A good file exits 0; a bad one exits 2 with ``file:line:`` lines."""
        good = write_config(tmp_path)
        assert main(["validate", str(good)]) == OK
        assert "ok" in capsys.readouterr().out
        bad = write_config(tmp_path, "jobs:\n  a:\n    schedule: manual\n")
        assert main(["validate", str(bad)]) == USAGE
        out = capsys.readouterr().out
        assert f"{bad}:" in out
        assert all(line.startswith(f"{bad}:") for line in out.splitlines())

    def test_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A file that does not exist is a validation error."""
        assert main(["validate", str(tmp_path / "nope.yaml")]) == USAGE
        assert "nope.yaml" in capsys.readouterr().out


class TestForecast:
    """``pysched forecast FILE`` predicts runs from the config alone."""

    def test_forecast_offline(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """List the firings and their dependency-only followers in the window."""
        path = write_config(tmp_path, FORECAST_JOBS)
        argv = ["forecast", str(path), "--from", "2026-10-19", "--to", "2026-10-20"]
        assert main(argv) == OK
        header, *rows = capsys.readouterr().out.splitlines()
        assert header.split() == ["JOB", "RUN_DATE", "EXPECTED_START", "VIA"]
        assert [row.split()[:3] for row in rows] == [
            ["extract", "2026-10-19", "2026-10-19T18:30:00Z"],
            ["load", "2026-10-19", "2026-10-19T18:30:00Z"],
            ["extract", "2026-10-20", "2026-10-20T18:30:00Z"],
            ["load", "2026-10-20", "2026-10-20T18:30:00Z"],
        ]

    def test_bad_window(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A window that ends before it starts is a usage error."""
        path = write_config(tmp_path, FORECAST_JOBS)
        argv = ["forecast", str(path), "--from", "2026-10-20", "--to", "2026-10-18"]
        assert main(argv) == USAGE
        assert "before" in capsys.readouterr().err


class TestOfflineMisc:
    """``hash-token`` and usage errors."""

    def test_hash_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print a new token and its SHA-256 for the tokens file."""
        assert main(["hash-token"]) == OK
        lines = dict(line.split(": ", 1) for line in capsys.readouterr().out.splitlines())
        assert len(lines["token"]) >= TOKEN_BYTES
        assert len(lines["token_sha256"]) == SHA256_HEX
        assert hashlib.sha256(lines["token"].encode()).hexdigest() == lines["token_sha256"]

    def test_no_command_is_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No subcommand exits 2."""
        assert main([]) == USAGE
        assert "usage" in capsys.readouterr().err

    def test_unreachable_server(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A server that is not there is a server error (exit 1)."""
        assert main(["status", "--url", "http://127.0.0.1:1"]) == SERVER_ERROR
        assert "cannot reach scheduler" in capsys.readouterr().err


class TestAgainstServer:
    """The client commands talk to a running ``serve``."""

    def test_cli_against_server(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``rerun``, ``hold`` and ``release`` return 0; an unknown run gives 1 with the message."""
        path = write_config(tmp_path)
        a_run = f"a.{today()}.1"
        waiter_run = f"waiter.{today()}.1"

        def body(url: str) -> list[int]:
            """Run the commands; return their exit codes."""
            client = ApiClient(url, None)
            codes = [main(["--url", url, "trigger", "a"])]
            wait_for_states(client, {"a": "succeeded", "b": "succeeded"})
            codes.append(main(["rerun", "--url", url, a_run]))
            wait_for_states(client, {"a": "succeeded"})
            codes.append(main(["trigger", "--url", url, "waiter"]))
            codes.append(main(["hold", "--url", url, waiter_run]))
            assert client.run(waiter_run)["run"]["state"] == "held"
            codes.append(main(["release", "--url", url, waiter_run]))
            assert client.run(waiter_run)["run"]["state"] != "held"
            codes.append(main(["show", "--url", url, a_run]))
            codes.append(main(["log", "--url", url, a_run, "--tail", "5"]))
            codes.append(main(["runs", "--url", url, "--job", "a", "--limit", "5"]))
            codes.append(main(["reload", "--url", url]))
            write_config(tmp_path, "jobs:\n  broken:\n    schedule: manual\n")
            codes.append(main(["reload", "--url", url]))
            codes.append(main(["hold", "--url", url, "nope.2026-10-19.1"]))
            return codes

        codes = with_service(path, body)
        captured = capsys.readouterr()
        assert codes[:-2] == [OK] * (len(codes) - 2)
        assert codes[-2:] == [SERVER_ERROR, SERVER_ERROR]
        assert "config has problems" in captured.err
        assert f"  {path}:" in captured.err
        assert "nope.2026-10-19.1" in captured.err
        assert "attempts:" in captured.out
        assert "events:" in captured.out
