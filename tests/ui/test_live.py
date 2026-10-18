"""Test the live feed: ``/events`` with ids, replay after a gap, and ``reset``."""

import json
from datetime import timedelta
from typing import Any

import pynet.loop
from api_helpers import BOOT, DEV, MakeApi, users_yaml
from pyweb.sse import ServerEvent
from ui_helpers import next_event, open_stream

from pyscheduler.ui.live import KEEPALIVE, LIVE_BUFFER, LiveFeed

SEE_OTHER = 303
UNAUTHORISED = 401
SMALL_BUFFER = 5
PUBLISHED = 8
"""With a buffer of five, events 4 to 8 are still held after eight."""
BOOT_HEX = 8
ADHOC = "adhoc.2026-10-19.1"
TWO_MINUTES = timedelta(minutes=2)
STALE = (f"{BOOT}-2", "other-5", "garbage", f"{BOOT}-x", f"{BOOT}-\u00b2", "-", f"{BOOT}-99")
"""Ids a buffer of 4..8 cannot resume from: too old, another boot, garbage, the future."""
OK = 200


def _data(event: ServerEvent) -> dict[str, Any]:
    """Return the event's JSON data."""
    found: dict[str, Any] = json.loads(event.data)
    return found


def _feed(count: int = PUBLISHED) -> LiveFeed:
    """Return a small feed (buffer ``SMALL_BUFFER``) that has published ``count`` run events."""
    feed = LiveFeed(buffer=SMALL_BUFFER, boot=BOOT)
    for n in range(1, count + 1):
        assert feed.publish("run", {"n": n}) == f"{BOOT}-{n}"
    return feed


def _numbers(events: list[ServerEvent]) -> list[int]:
    """Return the ``n`` of each event's data."""
    return [_data(event)["n"] for event in events]


class TestLiveFeed:
    """The feed numbers its events, replays after a gap and says ``reset`` when it cannot."""

    def test_defaults(self) -> None:
        """A feed has a random boot id of eight hex digits and the plan's constants."""
        assert LIVE_BUFFER == 1000  # noqa: PLR2004 - the value the plan fixes
        assert KEEPALIVE == 15.0  # noqa: PLR2004 - the value the plan fixes
        first, second = LiveFeed(), LiveFeed()
        assert len(first.boot) == BOOT_HEX
        int(first.boot, 16)
        assert first.boot != second.boot
        assert first.last_id == f"{first.boot}-0"
        assert first.stream(None).keepalive == KEEPALIVE

    def test_replay_and_reset(self) -> None:
        """Replay what is still buffered; a stale, foreign or garbage id gets one ``reset``."""

        async def main() -> None:
            """Reconnect with each kind of ``Last-Event-ID``."""
            feed = _feed()
            stream = feed.stream(f"{BOOT}-3")
            replayed = [await anext(stream.source) for _ in range(PUBLISHED - 3)]
            assert _numbers(replayed) == [4, 5, 6, 7, 8]
            assert [event.id for event in replayed] == [f"{BOOT}-{n}" for n in range(4, 9)]
            assert {event.event for event in replayed} == {"run"}
            feed.publish("run", {"n": 9})  # then it carries on live
            assert _numbers([await anext(stream.source)]) == [9]
            await stream.aclose()
            assert feed.listeners == 0
            for stale in STALE:
                late = feed.stream(stale)
                reset = await anext(late.source)
                assert (reset.event, reset.data, reset.id) == ("reset", "{}", feed.last_id), stale
                feed.publish("run", {"n": 0})
                after = await anext(late.source)
                assert after.event == "run", stale  # only one reset, then live events
                await late.aclose()
            assert feed.listeners == 0

        pynet.loop.run(main())

    def test_live_after_subscribe_no_gap_or_repeat(self) -> None:
        """Events published while a replay is read come once each, in order."""

        async def main() -> None:
            """Read part of the replay, publish more, then read the rest."""
            feed = _feed()
            current = feed.stream(f"{BOOT}-{PUBLISHED}")
            caught_up = feed.stream(f"{BOOT}-6")
            first = await anext(caught_up.source)  # the replay of 7 and 8 is snapshotted now
            feed.publish("run", {"n": 9})
            feed.publish("run", {"n": 10})
            rest = [await anext(caught_up.source) for _ in range(3)]
            assert _numbers([first, *rest]) == [7, 8, 9, 10]
            assert _numbers([await anext(current.source)]) == [9]
            plain = feed.stream(None)  # no id: live events only
            pending = pynet.loop.spawn(next_event(plain))
            await pynet.loop.sleep(0)
            feed.publish("run", {"n": 11})
            assert _numbers([await pending]) == [11]
            for stream in (current, caught_up, plain):
                await stream.aclose()
            assert feed.listeners == 0

        pynet.loop.run(main())

    def test_slow_listener_gets_reset(self) -> None:
        """A listener more than a buffer behind gets one ``reset``, then the newer events."""

        async def main() -> None:
            """Hear one event, then let a buffer and more pile up unread, then read."""
            feed = _feed(0)
            stream = feed.stream(None)
            pending = pynet.loop.spawn(next_event(stream))
            await pynet.loop.sleep(0)  # the stream is listening now
            feed.publish("run", {"n": 0})
            assert _numbers([await pending]) == [0]
            for n in range(1, SMALL_BUFFER + 2):
                feed.publish("run", {"n": n})
            reset = await anext(stream.source)
            assert (reset.event, reset.id) == ("reset", f"{BOOT}-{SMALL_BUFFER + 2}")
            feed.publish("run", {"n": 99})
            assert _numbers([await anext(stream.source)]) == [99]
            await stream.aclose()

        pynet.loop.run(main())


class TestEventsRoute:
    """``GET /events`` streams the engine's run, attempt and reload changes."""

    def test_events_stream_run_changes(self, api: MakeApi) -> None:
        """Triggering a job gives ``run`` and ``attempt`` events in order, ids boot-1, boot-2..."""
        h = api(tokens=False, live=True)

        async def body() -> None:
            """Watch live, trigger ``adhoc``, let it finish, then replay everything."""
            assert h.live is not None
            watching = await open_stream(h, "/events", DEV)
            pending = pynet.loop.spawn(next_event(watching))
            await pynet.loop.sleep(0)
            await h.client.post("/api/jobs/adhoc/trigger", headers=DEV)
            await h.advance_to(h.clock.now() + TWO_MINUTES)
            live_first = await pending
            await watching.aclose()
            seen = int(h.live.last_id.rpartition("-")[2])
            events = await h.client.stream(
                "/events", count=seen, headers={**DEV, "Last-Event-ID": f"{BOOT}-0"}
            )
            assert [event.id for event in events] == [f"{BOOT}-{n}" for n in range(1, seen + 1)]
            assert live_first in events
            runs = [_data(e) for e in events if e.event == "run" and _data(e)["run_id"] == ADHOC]
            assert {tuple(sorted(run)) for run in runs} == {("job", "run_date", "run_id", "state")}
            states = [run["state"] for run in runs]
            assert states[-1] == "succeeded"
            assert states.index("running") < states.index("succeeded")
            assert {run["run_date"] for run in runs} == {"2026-10-19"}
            attempts = [_data(e) for e in events if e.event == "attempt"]
            assert attempts == [
                {"run_id": ADHOC, "attempt": 1, "ended": False},
                {"run_id": ADHOC, "attempt": 1, "ended": True},
            ]
            later = await h.client.stream(
                "/events", count=2, headers={**DEV, "Last-Event-ID": f"{BOOT}-{seen - 2}"}
            )
            assert later == events[-2:]

        h.run(body)

    def test_reload_event(self, api: MakeApi) -> None:
        """A reload that changes the config gives a ``reload`` event with the new version."""
        h = api(tokens=False, live=True)

        async def body() -> None:
            """Add a job to the config file and reload."""
            assert h.live is not None
            with h.config_path.open("a", encoding="utf-8") as handle:
                handle.write("  extra:\n    command: run-extra\n    schedule: manual\n")
            response = await h.client.post("/api/reload", headers=DEV)
            assert response.status == OK, response.text
            last = int(h.live.last_id.rpartition("-")[2])
            (event,) = await h.client.stream(
                "/events", count=1, headers={**DEV, "Last-Event-ID": f"{BOOT}-{last - 1}"}
            )
            assert (event.event, _data(event)) == ("reload", {"version": 2})

        h.run(body)

    def test_detach_stops_events(self) -> None:
        """After ``detach`` the engine's changes no longer reach the feed."""

        class Engine:
            """Just the three listener hooks ``attach`` uses."""

            config_version = 3

            def __init__(self) -> None:
                """Start with no listeners."""
                self.hooks: list[Any] = []

            def _hook(self, callback: Any) -> Any:
                """Remember ``callback``; return the unsubscribe."""
                self.hooks.append(callback)
                return lambda: self.hooks.remove(callback)

            subscribe = watch_attempts = watch_reloads = _hook

        async def main() -> None:
            """Attach, hear a reload, detach."""
            engine = Engine()
            feed = LiveFeed(boot=BOOT)
            detach = feed.attach(engine)  # type: ignore[arg-type]
            assert len(engine.hooks) == len(("run", "attempt", "reload"))
            engine.hooks[2]()
            (event,) = [await anext(feed.stream(f"{BOOT}-0").source)]
            assert (event.event, _data(event), event.id) == ("reload", {"version": 3}, f"{BOOT}-1")
            detach()
            assert engine.hooks == []

        pynet.loop.run(main())

    def test_events_need_login(self, api: MakeApi) -> None:
        """Logged out gives 401 JSON, or a 303 to the login page when HTML is wanted."""
        h = api(tokens=False, users=users_yaml(vic="viewer"), live=True)

        async def body() -> None:
            """Ask for the feed without a session."""
            plain = await h.client.get("/events")
            assert plain.status == UNAUTHORISED
            assert plain.json() == {"error": "unauthorised"}
            page = await h.client.get("/events", headers={"Accept": "text/html"})
            assert page.status == SEE_OTHER
            assert (page.headers.get("Location") or "").startswith("/login")

        h.run(body)
