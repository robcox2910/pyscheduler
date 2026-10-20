"""Test the log sources: files here, an agent's logs over the hub, and how each fails."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pynet.loop
import pytest

from pyscheduler.agents.logsource import (
    NO_ANSWER,
    OFFLINE,
    LocalLogSource,
    LogUnavailableError,
    RemoteLogSource,
    log_source_for,
)
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.model import Attempt
from pyscheduler.fake_hub import FakeHub

AGENT = "build-01"
RUN_ID = "nightly.2026-10-19.1"
FIRST = 1
SECOND = 2
WANTED = 2
LIMIT = 4
WHOLE = 8
NOON = datetime(2026, 10, 19, 12, tzinfo=UTC)


def _row(**changes: Any) -> Attempt:
    """Return an attempt row of ``RUN_ID`` with ``changes``."""
    row = Attempt(RUN_ID, FIRST, NOON, None, None, None, None, None, "", "", "", "")
    return replace(row, **changes)


def _hub() -> FakeHub:
    """Return a fake hub with ``AGENT`` online and a three-line stdout log."""
    hub = FakeHub(FakeClock(NOON))
    hub.connect(AGENT)
    hub.logs[RUN_ID, FIRST, "stdout"] = b"a\nb\nc\n"
    return hub


def _source(
    hub: FakeHub | None, *, attempt: int = FIRST, stream: str = "stdout"
) -> RemoteLogSource:
    """Return a remote source for ``AGENT``'s log."""
    return RemoteLogSource(hub, AGENT, RUN_ID, attempt, stream)


class TestLocalLogSource:
    """A local source reads a file on a helper thread."""

    def test_tail_first_look_and_read(self, tmp_path: Path) -> None:
        """Answer all three questions from one file, and a missing file as nothing yet."""
        path = tmp_path / "out.log"
        source = LocalLogSource(path)

        async def body() -> None:
            """Ask before the file exists, then after."""
            assert await source.tail(WANTED) is None
            assert await source.first_look(WANTED) == ([], 0)
            assert await source.read(0, LIMIT) == (b"", 0, False)
            path.write_bytes(b"a\nb\nc\nhalf")
            assert await source.tail(WANTED) == (["c", "half"], True)
            assert await source.first_look(WANTED) == (["b", "c"], len(b"a\nb\nc\n"))
            assert await source.read(2, LIMIT) == (b"b\nc\n", 2 + LIMIT, False)

        pynet.loop.run(body())


class TestRemoteLogSource:
    """A remote source asks the agent through the hub."""

    def test_answers(self) -> None:
        """Tail, first look and read come from the agent's log."""
        source = _source(_hub())

        async def body() -> None:
            """Ask the three questions."""
            assert await source.tail(WANTED) == (["b", "c"], True)
            assert await source.first_look(WANTED) == (["b", "c"], len(b"a\nb\nc\n"))
            assert await source.read(2, LIMIT) == (b"b\nc\n", 2 + LIMIT, False)
            assert await source.read(WHOLE * WHOLE, LIMIT) == (b"a\nb\n", LIMIT, True)  # shrank

        pynet.loop.run(body())

    def test_missing_log_is_nothing_yet(self) -> None:
        """A log the agent does not have reads like a local file that is not there."""
        source = _source(_hub(), stream="stderr")

        async def body() -> None:
            """Ask for a stream with no log."""
            assert await source.tail(WANTED) is None
            assert await source.first_look(WANTED) == ([], 0)
            assert await source.read(WHOLE, LIMIT) == (b"", WHOLE, False)

        pynet.loop.run(body())

    def test_unreachable_agents(self) -> None:
        """Offline, silent and hub-less agents all raise ``LogUnavailableError`` with the reason."""
        hub = _hub()
        hub.silent.add(AGENT)
        cases = [
            (_source(hub), NO_ANSWER),
            (RemoteLogSource(_hub(), "stranger", RUN_ID, FIRST, "stdout"), OFFLINE),
            (_source(None), OFFLINE),
        ]

        async def body() -> None:
            """Each source fails with its reason."""
            for source, reason in cases:
                with pytest.raises(LogUnavailableError, match=reason):
                    await source.tail(WANTED)
                with pytest.raises(LogUnavailableError, match=reason):
                    await source.read(0, LIMIT)
                with pytest.raises(LogUnavailableError, match=reason):
                    await source.first_look(WANTED)

        pynet.loop.run(body())


class TestLogSourceFor:
    """The row says which source reads its log."""

    def test_local_and_remote_rows(self, tmp_path: Path) -> None:
        """A plain row reads its file; an ``agent:`` row asks that agent."""
        engine: Any = type("E", (), {"placement": _hub()})()
        local = log_source_for(
            engine, _row(stdout_path=str(tmp_path / "o"), stderr_path="e"), "stdout"
        )
        assert isinstance(local, LocalLogSource)
        assert local.path == tmp_path / "o"
        remote = log_source_for(engine, _row(status_path=f"agent:{AGENT}"), "stderr")
        assert isinstance(remote, RemoteLogSource)
        assert (remote.agent, remote.attempt, remote.stream) == (AGENT, FIRST, "stderr")
