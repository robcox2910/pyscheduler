"""Tests for signed agent messages: sealing, verifying, replay and staleness."""

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pynet.loop
import pytest
from pynet.messaging import JsonValue

from pyscheduler.agents import MAX_SKEW, NONCE_TTL, Rejected, Verifier, seal, sign
from pyscheduler.agents.messages import (
    REPORT_TYPES,
    SCHEDULER_TYPES,
    kill_payload,
    message_id,
    parse_kill,
    work_queue,
)
from pyscheduler.engine.clock import FakeClock

START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SECRET = "s" * 40
OTHER = "o" * 40
AGENT = "build-01"
TEN_MINUTES = timedelta(minutes=10)
SIG_LENGTH = 64
PAYLOAD: dict[str, JsonValue] = {"run_id": "nightly.2026-10-20", "attempt": 1}


def _verifier(clock: FakeClock) -> Verifier:
    """Return a verifier that knows ``build-01``."""
    return Verifier({AGENT: SECRET}.get, clock=clock, accept=SCHEDULER_TYPES)


def _sealed(
    clock: FakeClock, secret: str = SECRET, agent: str = AGENT, at: datetime | None = None
) -> tuple[dict[str, JsonValue], dict[str, str]]:
    """Return a work message sealed at ``at`` (default now)."""
    return seal("work", agent, PAYLOAD, secret, now=at or clock.now())


class TestRoundTrip:
    """A sealed message verifies and comes back unchanged."""

    def test_round_trip(self) -> None:
        """Keep the kind, agent and payload through seal and check."""
        clock = FakeClock(START)
        body, headers = _sealed(clock)
        message = _verifier(clock).check(body, headers)
        assert (message.kind, message.agent, message.payload) == ("work", AGENT, PAYLOAD)
        assert message.sent_at == START
        assert len(headers["sig"]) == SIG_LENGTH

    def test_sign_is_stable(self) -> None:
        """Sign the same body to the same digest whatever the key order."""
        assert sign({"a": 1, "b": 2}, SECRET) == sign({"b": 2, "a": 1}, SECRET)
        assert sign({"a": 1}, SECRET) != sign({"a": 1}, OTHER)


class TestRejects:
    """Every failure raises ``Rejected`` with a short reason."""

    @pytest.mark.parametrize(
        ("case", "reason"),
        [
            ("wrong-secret", "bad signature"),
            ("tampered", "bad signature"),
            ("no-sig", "missing sig"),
            ("string", "not an object"),
            ("list", "not an object"),
            ("no-nonce", "malformed"),
        ],
    )
    def test_verify_rejects(self, case: str, reason: str) -> None:
        """Reject a doctored message, saying why."""
        clock = FakeClock(START)
        body, headers = _sealed(clock)
        bad_body: JsonValue = body
        bad_headers = headers
        if case == "wrong-secret":
            bad_headers = {"sig": sign(body, OTHER)}
        elif case == "tampered":
            bad_body = {**body, "payload": {"run_id": "evil"}}
        elif case == "no-sig":
            bad_headers = {}
        elif case == "string":
            bad_body = "nope"
        elif case == "list":
            bad_body = [1, 2]
        else:
            bad_body = {k: v for k, v in body.items() if k != "nonce"}
        with pytest.raises(Rejected) as info:
            _verifier(clock).check(bad_body, bad_headers)
        assert info.value.reason == reason

    @pytest.mark.parametrize(
        ("offset", "reason"),
        [(-TEN_MINUTES, "stale"), (TEN_MINUTES, "stale")],
        ids=["ten-minutes-ago", "ten-minutes-ahead"],
    )
    def test_clock_skew(self, offset: timedelta, reason: str) -> None:
        """Reject a message sent too long ago or too far ahead."""
        clock = FakeClock(START)
        body, headers = _sealed(clock, at=START + offset)
        with pytest.raises(Rejected) as info:
            _verifier(clock).check(body, headers)
        assert info.value.reason == reason

    def test_edge_of_skew_is_accepted(self) -> None:
        """Accept a message exactly ``MAX_SKEW`` old."""
        clock = FakeClock(START)
        body, headers = _sealed(clock, at=START - MAX_SKEW)
        assert _verifier(clock).check(body, headers).kind == "work"

    def test_replay(self) -> None:
        """Reject the same message the second time."""
        clock = FakeClock(START)
        verifier = _verifier(clock)
        body, headers = _sealed(clock)
        verifier.check(body, headers)
        with pytest.raises(Rejected) as info:
            verifier.check(body, headers)
        assert info.value.reason == "replayed nonce"

    def test_unknown_agent(self) -> None:
        """Reject a message from an agent not in the secrets."""
        clock = FakeClock(START)
        body, headers = _sealed(clock, agent="ghost")
        with pytest.raises(Rejected) as info:
            _verifier(clock).check(body, headers)
        assert info.value.reason == "unknown agent"

    def test_unexpected_type(self) -> None:
        """Refuse a validly sealed work message when only reports are accepted."""
        clock = FakeClock(START)
        verifier = Verifier({AGENT: SECRET}.get, clock=clock, accept=REPORT_TYPES)
        body, headers = _sealed(clock)
        with pytest.raises(Rejected) as info:
            verifier.check(body, headers)
        assert info.value.reason == "unexpected type"
        assert verifier.remembered == 0

    def test_non_string_sig(self) -> None:
        """Refuse a sig header that is not text without crashing."""
        clock = FakeClock(START)
        body, _ = _sealed(clock)
        with pytest.raises(Rejected) as info:
            _verifier(clock).check(body, cast("dict[str, str]", {"sig": 5}))
        assert info.value.reason == "missing sig"

    def test_naive_seal(self) -> None:
        """Refuse to seal a naive time, and send other zones as UTC."""
        with pytest.raises(ValueError, match="aware"):
            seal("work", AGENT, PAYLOAD, SECRET, now=datetime.fromisoformat("2026-10-20T12:00:00"))
        zone = timezone(timedelta(hours=2))
        body, _ = seal("work", AGENT, PAYLOAD, SECRET, now=START.astimezone(zone))
        assert body["sent_at"] == START.isoformat()

    def test_reason_leaks_nothing(self) -> None:
        """Keep the secret and payload out of every reason."""
        clock = FakeClock(START)
        body, headers = _sealed(clock, secret=OTHER)
        with pytest.raises(Rejected) as info:
            _verifier(clock).check(body, headers)
        text = f"{info.value} {info.value!r} {info.value.reason}"
        assert SECRET not in text
        assert OTHER not in text
        assert "nightly" not in text


class TestNonceMemory:
    """The nonce memory is pruned once its time is up."""

    def test_nonce_memory_pruned(self) -> None:
        """Forget nonces after ``NONCE_TTL`` so the set stops growing."""
        clock = FakeClock(START)
        verifier = _verifier(clock)
        count = 5

        async def main() -> None:
            """Check messages, jump past the TTL and check again."""
            for _ in range(count):
                verifier.check(*_sealed(clock))
            assert verifier.remembered == count
            await clock.advance(NONCE_TTL + timedelta(seconds=1))
            verifier.check(*_sealed(clock))
            assert verifier.remembered == 1

        pynet.loop.run(main())

    def test_no_replay_at_old_boundary(self) -> None:
        """Reject a replay at start + NONCE_TTL of a message sent at the skew limit."""
        clock = FakeClock(START)
        verifier = _verifier(clock)
        body, headers = _sealed(clock, at=START + MAX_SKEW)
        verifier.check(body, headers)

        async def main() -> None:
            """Replay at the skew limit and the old boundary; a second later it is stale."""
            await clock.advance(MAX_SKEW)
            with pytest.raises(Rejected) as info:
                verifier.check(body, headers)
            assert info.value.reason == "replayed nonce"
            await clock.advance(NONCE_TTL - MAX_SKEW)
            with pytest.raises(Rejected) as info:
                verifier.check(body, headers)
            assert info.value.reason == "replayed nonce"
            await clock.advance(timedelta(seconds=1))
            with pytest.raises(Rejected) as info:
                verifier.check(body, headers)
            assert info.value.reason == "stale"

        pynet.loop.run(main())


class TestMessages:
    """The small helpers in ``agents.messages``."""

    def test_names(self) -> None:
        """Build the queue and message names."""
        assert work_queue(AGENT) == "agent.build-01.work"
        assert message_id("r1", 2) == "r1.2"

    def test_kill_round_trip(self) -> None:
        """Build and parse a kill payload."""
        assert parse_kill(kill_payload("r1", 2, "TERM")) == ("r1", 2, "TERM")

    def test_kill_rejects_bad(self) -> None:
        """Refuse a bad signal name and a malformed payload."""
        with pytest.raises(ValueError, match="signal"):
            kill_payload("r1", 1, "HUP")
        with pytest.raises(Rejected):
            parse_kill({"run_id": "r1", "attempt": True, "signal": "TERM"})
