"""A run made because its upstream was triggered is the scheduler's doing, not the operator's."""

import json
from datetime import timedelta

import pynet.loop
from engine_helpers import TODAY, MakeHarness

from pyscheduler.engine.model import Event

ALICE = "alice"
CHAIN = """
jobs:
  a: {command: run-a, schedule: manual}
  b:
    command: run-b
    depends_on: [a]
"""


def _events(h_events: list[Event], run_id: str) -> list[tuple[str, str, dict[str, object]]]:
    """Return ``(actor, action, detail)`` of ``run_id``'s events."""
    return [(e.actor, e.action, json.loads(e.detail)) for e in h_events if e.run_id == run_id]


class TestDependencyAudit:
    """Dependency runs carry ``created`` by the scheduler, naming the upstream run."""

    def test_trigger_and_backfill(self, harness: MakeHarness) -> None:
        """Trigger and backfill ``a``: each ``b`` is created by the scheduler because of ``a``."""
        h = harness(CHAIN)
        events: list[Event] = []

        async def body() -> None:
            """Trigger today, backfill yesterday, then read the logbook."""
            made = await h.engine.trigger("a", actor=ALICE)
            yesterday = TODAY - timedelta(days=1)
            filled = await h.engine.backfill("a", start=yesterday, end=yesterday, actor=ALICE)
            await h.engine.settle()
            for upstream, day in ((made.run_id, TODAY), (filled[0].run_id, yesterday)):
                down = f"b.{day.isoformat()}.1"
                events.extend(await pynet.loop.run_in_thread(h.store.events_for, down))
                found = _events(events, down)
                assert all(actor == "scheduler" for actor, _, _ in found), found
                created = [detail for _, action, detail in found if action == "created"]
                assert created == [{"trigger": "dependency", "because": upstream}]

        h.run_engine(body)
