"""Test that a users or tokens reload ends every open live stream and log tail (ruling FR2)."""

from datetime import timedelta
from pathlib import Path

import pynet.loop
import pytest
from api_helpers import (
    OK,
    OPERATOR,
    UNAUTHORISED,
    VIEWER_KEY,
    ApiHarness,
    MakeApi,
    log_in,
    sha,
    users_yaml,
)
from pyweb.sse import EventStream
from ui_helpers import next_event, open_stream

from pyscheduler.testing import FakeResult
from pyscheduler.ui import logtail
from pyscheduler.ui.live import LiveFeed
from pyscheduler.ui.logtail import LogTails

QUICK_POLL = 0.01
"""A test's log poll interval (real seconds), instead of ``LOG_POLL``."""
ADHOC = "adhoc.2026-10-19.1"
LONG = timedelta(minutes=10)


async def _ended(stream: EventStream) -> bool:
    """Return whether ``stream`` ends (rather than sending another event) soon."""
    try:
        event = await next_event(stream)
    except StopAsyncIteration:
        return True
    msg = f"the stream sent {event} instead of ending"
    raise AssertionError(msg)


async def _listening(feed: LiveFeed, stream: EventStream) -> None:
    """Publish one event and read it, so ``stream`` is a listener (as a served stream is)."""
    feed.publish("run", {"n": 1})
    assert (await next_event(stream)).event == "run"


async def _reload_with_only(h: ApiHarness, kept: str) -> None:
    """Rewrite the users file with just ``kept`` (an operator) and reload."""
    (h.tmp_path / "users.yaml").write_text(users_yaml(**{kept: "operator"}), encoding="utf-8")
    response = await h.client.post("/api/reload", headers=OPERATOR)
    assert response.status == OK, response.text


async def _trigger(h: ApiHarness) -> None:
    """Trigger ``adhoc`` for a long fake run, with its stdout file made."""
    h.launcher.plan("adhoc", FakeResult(duration=LONG))
    made = await h.client.post("/api/jobs/adhoc/trigger", headers=OPERATOR)
    assert made.status == OK, made.text
    await h.engine.settle()
    (attempt,) = await pynet.loop.run_in_thread(h.store.attempts_for, ADHOC)
    path = Path(attempt.stdout_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hello\n", encoding="utf-8")


class TestRemovedUser:
    """A removed user's open streams end at the reload, and reconnecting is refused."""

    def test_live_stream_ends_and_reconnect_is_refused(self, api: MakeApi) -> None:
        """Open ``/events`` as vic, remove vic and reload: the stream ends; again gives 401."""
        h = api(users=users_yaml(vic="viewer", olga="operator"), live=True)

        async def body() -> None:
            """Open the feed as vic, reload without vic, and look again."""
            assert h.live is not None
            log_in(h, "vic")
            stream = await open_stream(h, "/events", {"Last-Event-ID": h.live.last_id})
            await _listening(h.live, stream)
            await _reload_with_only(h, "olga")
            assert await _ended(stream)
            again = await h.client.get("/events", headers={"Last-Event-ID": h.live.last_id})
            assert again.status == UNAUTHORISED
            await stream.aclose()

        h.run(body)

    def test_log_tail_ends(self, api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
        """An open log tail ends (without ``end``) at a users reload; reconnecting gives 401."""
        monkeypatch.setattr(logtail, "LOG_POLL", QUICK_POLL)
        h = api(users=users_yaml(vic="viewer", olga="operator"))

        async def body() -> None:
            """Tail adhoc's log as vic, reload without vic, and look again."""
            await _trigger(h)
            log_in(h, "vic")
            stream = await open_stream(h, f"/events/log/{ADHOC}")
            first = await next_event(stream)
            assert first.event == "lines"
            await _reload_with_only(h, "olga")
            assert await _ended(stream)
            again = await h.client.get(f"/events/log/{ADHOC}")
            assert again.status == UNAUTHORISED
            await stream.aclose()

        h.run(body)


class TestRevokedToken:
    """A revoked token's open ``curl /events`` ends at the tokens reload."""

    def test_bearer_stream_ends(self, api: MakeApi) -> None:
        """Open ``/events`` with the viewer token, drop it from the file and reload."""
        h = api(users=users_yaml(olga="operator"), live=True)

        async def body() -> None:
            """Stream with the viewer token, then revoke it."""
            assert h.live is not None
            bearer = {"Authorization": f"Bearer {VIEWER_KEY}"}
            stream = await open_stream(h, "/events", {**bearer, "Last-Event-ID": h.live.last_id})
            await _listening(h.live, stream)
            tokens = h.tmp_path / "tokens.yaml"
            kept = [line for line in tokens.read_text("utf-8").splitlines() if "viewer" not in line]
            tokens.write_text("\n".join(kept) + "\n", encoding="utf-8")
            assert sha(VIEWER_KEY) not in tokens.read_text("utf-8")
            response = await h.client.post("/api/reload", headers=OPERATOR)
            assert response.status == OK, response.text
            assert await _ended(stream)
            assert (await h.client.get("/events", headers=bearer)).status == UNAUTHORISED
            await stream.aclose()

        h.run(body)


class TestCloseAll:
    """``LiveFeed.close_all`` and ``LogTails.close_all`` end what is open, and only that."""

    def test_feed_close_all(self) -> None:
        """Every open listener ends; a stream opened afterwards still hears new events."""

        async def main() -> None:
            """Open two streams, close all, open a third."""
            feed = LiveFeed(boot="b")
            first, second = feed.stream("b-0"), feed.stream("b-0")
            feed.publish("run", {"n": 1})
            assert (await next_event(first)).id == "b-1"
            assert (await next_event(second)).id == "b-1"
            feed.close_all()
            assert await _ended(first)
            assert await _ended(second)
            assert feed.listeners == 0
            third = feed.stream("b-1")
            feed.publish("run", {"n": 2})
            assert (await next_event(third)).id == "b-2"
            await third.aclose()

        pynet.loop.run(main())

    def test_tails_close_all(self) -> None:
        """A tail's ticket is closed by ``close_all``; a later ticket is open."""
        tails = LogTails()
        ticket = tails.open()
        assert not ticket.closed
        tails.close_all()
        assert ticket.closed
        tails.release(ticket)
        assert not tails.open().closed
