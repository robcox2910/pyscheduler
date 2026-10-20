"""Test ``HubPlacement``: the engine's placement surface over a real ``AgentHub``."""

import dataclasses
from pathlib import Path

from agent_helpers import HOST, NAME, RUN_ID, now_clock
from hub_helpers import KEY, HubWorld, hub_config, run_hub, started_handle, work

from pyscheduler.agents.book import Refusal
from pyscheduler.agents.hub import AgentHub
from pyscheduler.agents.placement import HubPlacement
from pyscheduler.agents.remote_handle import RemoteHandle
from pyscheduler.config import JobDef
from pyscheduler.engine.remote import Placement

ONE_SLOT = 1


def _placement(tmp_path: Path) -> tuple[HubPlacement, JobDef]:
    """Return a placement over a hub that is not started, and a job pinned to ``NAME``."""
    config = hub_config(tmp_path, f'listen: "{HOST}:0"')
    assert config.agents is not None
    hub = AgentHub(config, clock=now_clock(), secrets=config.agents.secrets)
    placement = HubPlacement(hub)
    engine_side: Placement = placement  # the engine's protocol, checked by pyright
    assert engine_side is placement
    return placement, dataclasses.replace(config.jobs["nightly"], agent=NAME)


class TestHubPlacement:
    """The adapter chooses, books and frees through the hub's book."""

    def test_choose_reserve_release_watch(self, tmp_path: Path) -> None:
        """Refuse an offline agent, choose it once online, call the watcher on every change."""
        placement, job = _placement(tmp_path)
        changes: list[None] = []
        stop = placement.watch(lambda: changes.append(None))
        assert placement.choose(job) == Refusal(f"waiting for agent {NAME} (offline)")
        placement.hub.book.heard(NAME, max_running=ONE_SLOT)
        assert placement.choose(job) == NAME
        placement.reserve(NAME, KEY)
        assert placement.choose(job) == Refusal(f"waiting for agent {NAME} (busy)")
        placement.release(NAME, KEY)
        assert placement.choose(job) == NAME
        seen = len(changes)
        assert seen >= len(["heard", "reserve", "release"])
        stop()
        placement.reserve(NAME, KEY)
        assert len(changes) == seen

    def test_tags_choose_by_tags(self, tmp_path: Path) -> None:
        """Pass a job's tags to the book."""
        placement, job = _placement(tmp_path)
        tagged = dataclasses.replace(job, agent=None, agent_tags=("gpu",))
        assert placement.choose(tagged) == Refusal("waiting for an agent with tags gpu")
        placement.hub.book.heard(NAME, tags=["gpu"], max_running=ONE_SLOT)
        assert placement.choose(tagged) == NAME

    def test_dispatch_adopt_started_recovered(self, tmp_path: Path) -> None:
        """Send and adopt through the hub, and hear ``started`` through the adapter."""

        async def body(w: HubWorld) -> None:
            """Dispatch an attempt, see it start, adopt it again."""
            placement = HubPlacement(w.hub)
            heard: list[RemoteHandle] = []
            placement.watch_started(heard.append)
            handle = await started_handle(w)
            assert heard == [handle]
            assert placement.dispatch(NAME, work()) is handle
            assert placement.adopt(NAME, RUN_ID, 1, w.clock.now()) is handle
            placement.recovered()

        run_hub(tmp_path, body)
