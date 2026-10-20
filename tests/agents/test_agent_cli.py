"""Tests for the ``pysched-agent`` command line."""

import os
import signal
from pathlib import Path

import pynet.loop
import pytest
from agent_helpers import HOST, WAIT, Scheduler, make_agent, now_clock
from pymq.broker import BrokerServer

from pyscheduler.agents.cli import build_agent, main, read_secret, run_agent
from pyscheduler.agents.messages import HELLO_TYPE
from pyscheduler.engine.clock import SystemClock

USAGE = 2
FAILED = 1
GOOD = "g" * 40
SHORT = "too-short-a-secret"
PORT = 8471
DEFAULT_MAX_RUNNING = 4


def _secret_file(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    """Write ``text`` to a secret file with ``mode`` and return its path."""
    path = tmp_path / "secret"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _argv(secret: Path, *extra: str, name: str = "build-01") -> list[str]:
    """Return a command line using ``secret``."""
    return ["--broker", f"127.0.0.1:{PORT}", "--name", name, "--secret-file", str(secret), *extra]


class TestCliRefuses:
    """Bad input exits 2 with a message, and never prints the secret."""

    def test_cli_bad_secret_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Exit 2 for a missing or too-short secret file, without printing the secret."""
        assert main(_argv(tmp_path / "missing")) == USAGE
        assert "secret" in capsys.readouterr().err
        assert main(_argv(_secret_file(tmp_path, SHORT))) == USAGE
        err = capsys.readouterr().err
        assert "at least 32" in err
        assert SHORT not in err

    def test_bad_name_and_tags(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Exit 2 for a bad agent name, tag, broker or max-running."""
        secret = _secret_file(tmp_path, GOOD)
        assert main(_argv(secret, name="no/slash")) == USAGE
        assert main(_argv(secret, "--tags", "linux,bad tag")) == USAGE
        assert main(_argv(secret, "--max-running", "0")) == USAGE
        bad_broker = ["--broker", "nowhere", "--name", "b", "--secret-file", str(secret)]
        assert main(bad_broker) == USAGE
        assert GOOD not in capsys.readouterr().err

    def test_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Exit 2 when a required option is missing."""
        assert main(["--name", "build-01"]) == USAGE
        assert "--broker" in capsys.readouterr().err


class TestSecret:
    """The secret file holds one secret line."""

    def test_reads_and_strips(self, tmp_path: Path) -> None:
        """Return the secret without its trailing newline."""
        assert read_secret(_secret_file(tmp_path, GOOD + "\n")) == GOOD

    def test_warns_when_readable_by_others(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warn (without the secret) when the group or others may read the file."""
        read_secret(_secret_file(tmp_path, GOOD, 0o644))
        assert "readable" in caplog.text
        assert GOOD not in caplog.text


class TestBuild:
    """Good options build an agent with the documented defaults."""

    def test_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default the folders to ``./agent-state/<name>`` and ``./agent-logs/<name>``."""
        monkeypatch.chdir(tmp_path)
        secret = _secret_file(tmp_path, GOOD)
        agent = build_agent(_argv(secret, "--tags", "linux,gpu"), clock=SystemClock())
        assert agent.name == "build-01"
        assert agent.tags == {"linux", "gpu"}
        assert agent.broker == ("127.0.0.1", PORT)
        assert agent.max_running == DEFAULT_MAX_RUNNING
        assert agent.state_dir == Path("agent-state") / "build-01"
        assert agent.log_dir == Path("agent-logs") / "build-01"
        assert GOOD not in repr(agent)


class TestRun:
    """``main`` runs the agent until a stop signal; a failed start exits 1."""

    def test_failed_start_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 1 when the state folder cannot be made (it is a file)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a folder", encoding="utf-8")
        argv = _argv(_secret_file(tmp_path, GOOD), "--state-dir", str(blocker / "x"))
        assert main(argv) == FAILED
        err = capsys.readouterr().err
        assert "pysched-agent:" in err
        assert GOOD not in err

    def test_stops_on_sigterm(self, tmp_path: Path) -> None:
        """Say hello, then stop cleanly on SIGTERM."""

        async def body() -> None:
            """Run the agent against a real broker and signal ourselves once it is up."""
            server = BrokerServer()
            await server.start(HOST, 0)
            scheduler = Scheduler(server.port, now_clock())
            await scheduler.start()
            agent = make_agent(tmp_path, server.port, SystemClock())
            running = pynet.loop.spawn(run_agent(agent))
            try:
                await scheduler.wait_for(HELLO_TYPE)
                os.kill(os.getpid(), signal.SIGTERM)
                async with pynet.loop.timeout(WAIT):
                    await running
            finally:
                await scheduler.close()
                await server.close()

        pynet.loop.run(body())
