"""Tests for ``serve`` going wrong: a locked store, a busy port, signals while starting, failure."""

import logging
import os
import signal
import socket
import threading
import time
from pathlib import Path

import pynet.loop
import pytest

from pyscheduler.cli import main
from pyscheduler.client import ApiClient, ApiError
from pyscheduler.engine.core import Engine
from pyscheduler.engine.recovery import LAST_TICK
from pyscheduler.service import SchedulerFailedError, ServiceError, serve
from pyscheduler.store import Batch, Store
from service_helpers import DONE_SECONDS, POLL, reachable, with_service, write_config

SERVER_ERROR = 1
USAGE = 2
START_NAP = 0.05
"""How long the patched ``Engine.start`` waits for its own signals to arrive."""


class TestRefusedStarts:
    """A store that is in use, or a port that is taken, stops ``serve`` with one line."""

    def test_locked_state_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Another open store in the state folder exits 1 with one line on stderr."""
        path = write_config(tmp_path)
        store = Store.open(tmp_path / "state", sync=False)
        try:
            assert main(["serve", str(path)]) == SERVER_ERROR
        finally:
            store.close()
        err = capsys.readouterr().err
        assert err.startswith("pysched serve: ")
        assert len(err.splitlines()) == 1

    def test_busy_port(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A port in use is ``ServiceError`` (exit 2), and the engine never started."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            path = write_config(tmp_path, listen=f"127.0.0.1:{port}")
            with pytest.raises(ServiceError, match="cannot listen"):
                pynet.loop.run(serve(path))
            assert main(["serve", str(path)]) == USAGE
        assert "cannot listen" in capsys.readouterr().err
        store = Store.open(tmp_path / "state", sync=False)
        try:
            assert store.get_engine(LAST_TICK) is None  # no catch-up ever ran
        finally:
            store.close()

    def test_bad_log_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A log file that cannot be opened is a usage error."""
        path = write_config(tmp_path)
        missing = tmp_path / "no" / "such" / "dir" / "log.txt"
        assert main(["serve", str(path), "--log-file", str(missing)]) == USAGE
        assert "log file" in capsys.readouterr().err


class TestSignalsWhileStarting:
    """SIGTERM during start-up stops once the engine is up; SIGHUP then is ignored."""

    def test_sigterm_during_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Serve returns without ever being ready, and the handlers are put back."""
        caplog.set_level(logging.INFO, logger="pyscheduler.service")
        real_start = Engine.start

        async def start(self: Engine) -> None:
            """Get SIGHUP and SIGTERM while starting, then start for real."""
            os.kill(os.getpid(), signal.SIGHUP)
            os.kill(os.getpid(), signal.SIGTERM)
            await pynet.loop.sleep(START_NAP)
            await real_start(self)

        monkeypatch.setattr(Engine, "start", start)
        path = write_config(tmp_path)
        ports: list[int] = []
        before = signal.getsignal(signal.SIGTERM)
        pynet.loop.run(serve(path, ready=ports.append))
        assert ports == []
        assert signal.getsignal(signal.SIGTERM) is before
        assert "SIGHUP ignored: the scheduler is still starting" in caplog.messages
        assert "scheduler stopped" in caplog.messages


class TestFailureWhileServing:
    """A store commit that fails while serving ends ``serve`` with ``SchedulerFailedError``."""

    def test_engine_fails_while_serving(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Break the store, trigger a run; serve stops by itself and raises."""
        broken = threading.Event()
        real_apply = Store.apply

        def apply(self: Store, batch: Batch) -> None:
            """Fail every commit once ``broken`` is set."""
            if broken.is_set():
                msg = "disk full"
                raise OSError(msg)
            real_apply(self, batch)

        monkeypatch.setattr(Store, "apply", apply)
        path = write_config(tmp_path)

        def body(url: str) -> None:
            """Break the store, trigger ``a``, then wait for serve to go away."""
            client = ApiClient(url, None)
            broken.set()
            with pytest.raises(ApiError):
                client.trigger("a")
            deadline = time.monotonic() + DONE_SECONDS
            while time.monotonic() < deadline and reachable(client, allow_errors=True):
                time.sleep(POLL)
            assert not reachable(client, allow_errors=True)

        with pytest.raises(SchedulerFailedError, match="disk full"):
            with_service(path, body)
