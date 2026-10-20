"""Edge tests for the agent hub: broker binding, secrets, stopping and bad arguments."""

import dataclasses
import ipaddress
import socket
from pathlib import Path

import pytest
from agent_helpers import HOST, NAME, RUN_ID, SECRET, now_clock, until
from hub_helpers import HubWorld, hub_config, run_hub, secrets_with, started_handle, work

from pyscheduler.agents.book import Refusal
from pyscheduler.agents.hub import AgentHub
from pyscheduler.agents.hub_link import _dial_host

NEW_SECRET = "n" * 40
UNLISTED = "ghost-03"


def _hub(tmp_path: Path) -> AgentHub:
    """Return a hub (not started) listening on loopback."""
    config = hub_config(tmp_path, f'listen: "{HOST}:0"')
    assert config.agents is not None
    return AgentHub(config, clock=now_clock(), secrets=config.agents.secrets)


class TestArguments:
    """Bad calls are refused plainly."""

    def test_repr_and_address(self, tmp_path: Path) -> None:
        """Show the listen address but no secret; refuse ``address`` before start."""
        hub = _hub(tmp_path)
        assert SECRET not in repr(hub)
        assert HOST in repr(hub)
        with pytest.raises(RuntimeError, match="not started"):
            _ = hub.address

    def test_bad_dispatch_and_fetch(self, tmp_path: Path) -> None:
        """Refuse a payload without a run id, and log requests with bad arguments."""

        async def body(w: HubWorld) -> None:
            """Call with bad arguments."""
            with pytest.raises(ValueError, match="run_id"):
                w.hub.dispatch(NAME, {"job": "x"})
            with pytest.raises(ValueError, match="stream"):
                await w.hub.fetch_log(NAME, RUN_ID, 1, "both", mode="tail", count=1)
            with pytest.raises(ValueError, match="count"):
                await w.hub.fetch_log(NAME, RUN_ID, 1, "stdout", mode="tail")

        run_hub(tmp_path, body)

    def test_dispatch_twice_and_after_stop(self, tmp_path: Path) -> None:
        """Return the same handle for a repeat dispatch; refuse to dispatch once stopped."""

        async def body(w: HubWorld) -> None:
            """Dispatch twice, stop, dispatch again."""
            first = w.hub.dispatch(NAME, work())
            assert w.hub.dispatch(NAME, work()) is first
            assert w.hub.adopt(NAME, RUN_ID, 1, w.clock.now()) is first
            await w.hub.stop()
            with pytest.raises(RuntimeError, match="stopped"):
                w.hub.dispatch(NAME, work(attempt=2))

        run_hub(tmp_path, body)

    def test_no_secret_sends_nothing(self, tmp_path: Path) -> None:
        """Send nothing to an agent with no secret, and say so in the log."""

        async def body(w: HubWorld) -> None:
            """Dispatch to an agent missing from the secrets file."""
            handle = w.hub.dispatch(UNLISTED, work())
            await until(lambda: not w.hub._tasks)
            assert not handle.done

        run_hub(tmp_path, body)

    def test_dial_host(self) -> None:
        """Dial loopback for a wildcard listen address, else the address itself."""
        assert _dial_host(str(ipaddress.IPv4Address(0))) == HOST  # all interfaces
        assert _dial_host("::") == "::1"
        assert _dial_host("::1") == "::1"
        assert _dial_host("sched.local") == "sched.local"


class TestLifecycle:
    """Broker binding, secret reloads and stopping."""

    def test_embedded_broker_binds_loopback_by_default(self, tmp_path: Path) -> None:
        """Accept connections on loopback only when listening on ``127.0.0.1``."""
        lan = _lan_address()

        async def body(w: HubWorld) -> None:
            """Connect on loopback, and fail to connect on the LAN address."""
            host, port = w.hub.address
            assert host == HOST
            with socket.create_connection((HOST, port), timeout=1):
                pass
            with (
                pytest.raises(ConnectionRefusedError),
                socket.create_connection((lan, port), timeout=1),
            ):
                pass

        run_hub(tmp_path, body)

    def test_set_secrets_swaps_the_lookup(self, tmp_path: Path) -> None:
        """Verify and seal with the new secret after ``set_secrets``."""

        async def body(w: HubWorld) -> None:
            """Swap the secret; old-secret reports are refused, orders use the new one."""
            handle = await started_handle(w)
            w.hub.set_secrets(secrets_with(NEW_SECRET))
            await w.agent.finished()  # still sealed with the old secret
            await w.agent.finished(secret=NEW_SECRET)
            await handle.wait()
            await until(lambda: w.agent.refused == ["bad signature"])  # it trusts the old one

        run_hub(tmp_path, body)

    def test_stop_cancels_timers(self, tmp_path: Path) -> None:
        """Leave no live clock timer and no task behind after ``stop``."""

        async def body(w: HubWorld) -> None:
            """Stop the hub mid-attempt."""
            handle = await started_handle(w)
            handle.terminate()
            await w.hub.stop()
            assert w.clock.next_due() is None
            assert w.hub._tasks == set()
            handle.kill()  # after stop: nothing is sent, nothing is spawned
            assert w.hub._tasks == set()

        run_hub(tmp_path, body)

    def test_needs_an_agents_section(self, tmp_path: Path) -> None:
        """Refuse a config without ``agents:``."""
        config = hub_config(tmp_path, f'listen: "{HOST}:0"')
        bare = dataclasses.replace(config, agents=None)
        with pytest.raises(ValueError, match="agents"):
            AgentHub(bare, clock=now_clock(), secrets=secrets_with(SECRET))

    def test_book_places_on_the_hub(self, tmp_path: Path) -> None:
        """Expose the book, which refuses an unknown agent."""
        config = hub_config(tmp_path, f'listen: "{HOST}:0"')
        assert config.agents is not None
        hub = AgentHub(config, clock=now_clock(), secrets=config.agents.secrets)
        assert isinstance(hub.book.choose(agent=NAME, tags=frozenset()), Refusal)


def _lan_address() -> str:
    """Return this machine's non-loopback IPv4 address (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
        except OSError:
            pytest.skip("no route off this machine")
        address = str(probe.getsockname()[0])
    if address.startswith("127."):
        pytest.skip("no non-loopback address")
    return address
