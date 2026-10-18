"""Tests for the ``pysched`` command line: offline commands and commands against a server."""

import hashlib
from pathlib import Path

import pytest
from pyweb.security import verify_password

from pyscheduler import cli_commands
from pyscheduler.cli import main
from pyscheduler.client import ApiClient
from service_helpers import today, wait_for_states, with_service, write_config

OK = 0
SERVER_ERROR = 1
USAGE = 2
TOKEN_BYTES = 32
SHA256_HEX = 64
PROMPTS = 2

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

    def test_date_forms_and_instants(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A basic ISO date is a date; a value with a time is an instant (local without offset)."""
        path = write_config(tmp_path, FORECAST_JOBS)
        argv = ["forecast", str(path), "--from", "20261019", "--to", "2026-10-19T19:00"]
        assert main(argv) == OK
        rows = capsys.readouterr().out.splitlines()[1:]
        assert [row.split()[0] for row in rows] == ["extract", "load"]

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

    def test_hash_password_cli(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matching entries print a hash ``verify_password`` accepts; a mismatch exits 2."""
        answers = iter(["s3cret pass", "s3cret pass"])
        prompts: list[str] = []

        def fake_getpass(prompt: str = "Password: ") -> str:
            """Answer the next scripted entry and remember the prompt."""
            prompts.append(prompt)
            return next(answers)

        monkeypatch.setattr("getpass.getpass", fake_getpass)
        assert main(["hash-password"]) == OK
        out = capsys.readouterr().out
        label, _, digest = out.strip().partition(": ")
        assert label == "password_hash"
        assert digest.startswith("pbkdf2_sha256$")
        assert verify_password("s3cret pass", digest)
        assert len(prompts) == PROMPTS
        answers = iter(["one", "two"])
        assert main(["hash-password"]) == USAGE
        captured = capsys.readouterr()
        assert "passwords do not match" in captured.err
        assert captured.out == ""
        answers = iter(["", ""])
        assert main(["hash-password"]) == USAGE
        assert "empty" in capsys.readouterr().err

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
        day = today()  # once, and passed explicitly: midnight may pass during the test
        a_run = f"a.{day}.1"
        waiter_run = f"waiter.{day}.1"

        def body(url: str) -> list[int]:
            """Run the commands; return their exit codes."""
            client = ApiClient(url, None)
            codes = [main(["--url", url, "trigger", "a", "--run-date", day])]
            wait_for_states(client, {"a": "succeeded", "b": "succeeded"}, run_date=day)
            codes.append(main(["rerun", "--url", url, a_run]))
            wait_for_states(client, {"a": "succeeded"}, run_date=day)
            codes.append(main(["trigger", "--url", url, "waiter", "--date", day]))
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

    def test_tokens_through_the_cli(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No token is 401 and a viewer's action 403, each as exit 1 with the message."""
        viewer = "viewer-secret"
        digest = hashlib.sha256(viewer.encode()).hexdigest()
        tokens = tmp_path / "tokens.yaml"
        tokens.write_text(f"- {{name: vera, token_sha256: {digest}, role: viewer}}\n")
        path = write_config(tmp_path, extra=f"  api_tokens_file: {tokens}\n")

        def body(url: str) -> list[int]:
            """Ask without a token, then try an action as a viewer, then read as one."""
            return [
                main(["runs", "--url", url]),
                main(["trigger", "--url", url, "--token", viewer, "a"]),
                main(["runs", "--url", url, "--token", viewer]),
            ]

        assert with_service(path, body) == [SERVER_ERROR, SERVER_ERROR, OK]
        err = capsys.readouterr().err
        assert "pysched runs: 401: unauthorised" in err
        assert "pysched trigger: 403: forbidden" in err

    def test_status_says_when_truncated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the run list fills the page, ``status`` says only part is shown."""
        monkeypatch.setattr(cli_commands, "STATUS_LIMIT", 1)
        path = write_config(tmp_path)

        day = today()

        def body(url: str) -> int:
            """Make two runs, then ask for the status."""
            client = ApiClient(url, None)
            client.trigger("gate", run_date=day)
            client.trigger("gate", run_date=day)
            return main(["status", "--url", url, "--date", day])

        assert with_service(path, body) == OK
        assert "only the newest 1 runs" in capsys.readouterr().err
