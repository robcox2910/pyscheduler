"""Tests for ``serve``: the scheduler as a running service behind its HTTP API."""

import logging
import os
import signal
import time
from pathlib import Path

import pynet.loop
import pytest

from pyscheduler.cli import main
from pyscheduler.client import ApiClient, ApiError
from pyscheduler.config import ConfigError
from pyscheduler.engine.base import EngineFailedError
from pyscheduler.engine.core import Engine
from pyscheduler.service import SchedulerFailedError, ServiceError, is_loopback, serve
from pyscheduler.store import Store
from service_helpers import (
    DONE_SECONDS,
    POLL,
    idle,
    reachable,
    today,
    wait_for_states,
    with_service,
    write_config,
)

FIRST_VERSION = 1
SECOND_VERSION = 2
NOT_FOUND = 404


class TestServeEndToEnd:
    """``serve`` runs real jobs and answers the API on the port it was given."""

    def test_serve_end_to_end(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Trigger ``a``; ``a`` and then ``b`` succeed; ``status`` lists both; the log says so."""
        caplog.set_level(logging.INFO, logger="pyscheduler.service")
        path = write_config(tmp_path)

        def body(url: str) -> int:
            """Trigger a, wait for both runs, then print the status table."""
            client = ApiClient(url, None)
            assert client.health() == {"status": "ok", "timezone": "UTC", "today": today()}
            run = client.trigger("a")["run"]
            assert run["run_id"] == f"a.{today()}.1"
            wait_for_states(client, {"a": "succeeded", "b": "succeeded"})
            return main(["status", "--url", url])  # the server's today

        assert with_service(path, body) == 0
        out = capsys.readouterr().out
        header, *rows = out.splitlines()
        assert header.split() == ["JOB", "RUN", "STATE", "ATTEMPT", "STARTED", "DURATION"]
        assert {row.split()[0] for row in rows} == {"a", "b"}
        assert all(row.split()[2] == "succeeded" for row in rows)
        assert f"run a.{today()}.1 succeeded (attempt 1)" in caplog.messages
        assert f"run b.{today()}.1 succeeded (attempt 1)" in caplog.messages

    def test_records_the_config_once_per_digest(self, tmp_path: Path) -> None:
        """The first start records version 1; the same files again add nothing; a change adds 2."""
        path = write_config(tmp_path)
        with_service(path, idle)
        with_service(path, idle)
        store = Store.open(tmp_path / "state", sync=False)
        try:
            first = store.latest_config()
        finally:
            store.close()
        assert first is not None
        assert first.version == FIRST_VERSION
        write_config(tmp_path, "jobs:\n  solo:\n    command: 'true'\n    schedule: manual\n")
        with_service(path, idle)
        store = Store.open(tmp_path / "state", sync=False)
        try:
            second = store.latest_config()
        finally:
            store.close()
        assert second is not None
        assert second.version == SECOND_VERSION
        assert second.digest != first.digest


class TestSignals:
    """SIGHUP reloads the config files; SIGTERM ends ``serve`` gracefully."""

    def test_sighup_reloads_and_sigterm_stops(self, tmp_path: Path) -> None:
        """A job added on disk appears after SIGHUP; SIGTERM ends serve before ``stop`` is set."""
        path = write_config(tmp_path)

        def body(url: str) -> None:
            """Add a job, send SIGHUP, wait for it, then send SIGTERM and wait for the end."""
            client = ApiClient(url, None)
            with pytest.raises(ApiError) as caught:
                client.job("late")
            assert caught.value.status == NOT_FOUND
            write_config(tmp_path, "jobs:\n  late:\n    command: 'true'\n    schedule: manual\n")
            os.kill(os.getpid(), signal.SIGHUP)
            deadline = time.monotonic() + DONE_SECONDS
            names: list[str] = []
            while time.monotonic() < deadline:
                names = [job["name"] for job in client.jobs()["jobs"]]
                if names == ["late"]:
                    break
                time.sleep(POLL)
            assert names == ["late"]
            os.kill(os.getpid(), signal.SIGTERM)
            while time.monotonic() < deadline:
                if not reachable(client):
                    return
                time.sleep(POLL)
            msg = "serve did not stop on SIGTERM"
            raise AssertionError(msg)

        before = signal.getsignal(signal.SIGTERM)
        with_service(path, body)
        assert signal.getsignal(signal.SIGTERM) is before


class TestRefusals:
    """``serve`` refuses a bad config or an open address without tokens, before opening anything."""

    def test_serve_refuses_bad_config(self, tmp_path: Path) -> None:
        """A config with errors raises ``ConfigError`` and leaves no state folder behind."""
        path = write_config(tmp_path, "jobs:\n  a:\n    schedule: manual\n")
        with pytest.raises(ConfigError):
            pynet.loop.run(serve(path))
        assert not (tmp_path / "state").exists()

    def test_refuses_open_address_without_tokens(self, tmp_path: Path) -> None:
        """Listening on every address needs a tokens file (development mode is loopback only)."""
        path = write_config(tmp_path, listen="0.0.0.0:0")
        with pytest.raises(ServiceError, match="api_tokens_file"):
            pynet.loop.run(serve(path))
        assert not (tmp_path / "state").exists()

    def test_bad_tokens_file(self, tmp_path: Path) -> None:
        """A tokens file that cannot be read is refused before the store opens."""
        missing = tmp_path / "missing.yaml"
        path = write_config(tmp_path, extra=f"  api_tokens_file: {missing}\n")
        with pytest.raises(ServiceError, match="tokens file"):
            pynet.loop.run(serve(path))
        assert not (tmp_path / "state").exists()

    def test_main_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``pysched serve`` exits 2 with the problems for a bad config or a refused address."""
        bad = write_config(tmp_path, "jobs:\n  a:\n    schedule: manual\n")
        assert main(["serve", str(bad)]) == 2  # noqa: PLR2004 -- the usage exit code
        assert "pysched.yaml:" in capsys.readouterr().err
        open_host = write_config(tmp_path, listen="0.0.0.0:0")
        assert main(["serve", str(open_host)]) == 2  # noqa: PLR2004
        assert "api_tokens_file" in capsys.readouterr().err


class TestEngineFailure:
    """An engine that fails stop makes ``serve`` raise, and ``pysched serve`` exit 1."""

    def test_start_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A recovery commit that fails is ``SchedulerFailedError``; the store is closed again."""

        async def broken(self: Engine) -> None:
            """Fail like a recovery commit that could not be written."""
            del self
            msg = "disk full"
            raise EngineFailedError(msg)

        monkeypatch.setattr(Engine, "start", broken)
        path = write_config(tmp_path)
        with pytest.raises(SchedulerFailedError, match="disk full"):
            pynet.loop.run(serve(path))
        assert main(["serve", str(path)]) == 1
        assert "disk full" in capsys.readouterr().err


class TestLoopback:
    """``is_loopback`` tells loopback hosts from the rest."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.9.9.9", "::1", "[::1]", "localhost"])
    def test_loopback(self, host: str) -> None:
        """Count 127.0.0.0/8, ::1 and localhost as loopback."""
        assert is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.1", "example.com", ""])  # noqa: S104
    def test_not_loopback(self, host: str) -> None:
        """Count every other address or name as open."""
        assert not is_loopback(host)
