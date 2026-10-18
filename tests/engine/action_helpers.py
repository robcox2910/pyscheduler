"""Shared constants, configs and helpers of the operator action tests."""

import threading
from collections.abc import Callable
from datetime import date, timedelta

import pynet.loop
import pytest
from engine_helpers import START, Harness

from pyscheduler.engine.actions import ActionError
from pyscheduler.engine.model import Event
from pyscheduler.store import Batch

POLL = 0.005
"""How long (real seconds) to nap between looks at a gate."""

GATE_TIMEOUT = 5.0
"""How long (real seconds) a gated commit waits before going ahead anyway."""

ONE_MINUTE = timedelta(minutes=1)
TEN_MINUTES = timedelta(minutes=10)
FOUR_HOURS = timedelta(hours=4)
AT_1830 = START.replace(hour=18, minute=30)
ALICE = "alice"
DISK_FULL = "disk full"
NOT_FOUND = 404
CONFLICT = 409
BAD_REQUEST = 400
UNAVAILABLE = 503
TWO_ATTEMPTS = 2
THREE_ATTEMPTS = 3
FRIDAY = date(2026, 10, 16)
TUESDAY = date(2026, 10, 20)

CHAIN = """
jobs:
  a:
    command: run-a
    schedule: "30 18 * * *"
    retries: {count: 1, delay: 1m}
  b:
    command: run-b
    depends_on: [a]
"""

MANUAL = """
jobs:
  m:
    command: run-m
    schedule: manual
    retries: {count: 3, delay: 10m}
  n:
    command: run-n
    schedule: manual
    enabled: false
"""


def launched(h: Harness) -> list[str]:
    """Return the run ids launched so far, in order."""
    return [spec.run_id for spec in h.launcher.launched]


def actor_events(h: Harness, run_id: str) -> list[Event]:
    """Return the events of ``run_id`` that ``ALICE`` caused."""
    with h.reopened() as store:
        return [event for event in store.events_for(run_id) if event.actor == ALICE]


def status_of(error: pytest.ExceptionInfo[ActionError]) -> int:
    """Return the HTTP-style status of a caught action error."""
    return error.value.status


class Gate:
    """Hold up the first store commit that matches a test, until released."""

    def __init__(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch, matches: Callable[[Batch], bool]
    ) -> None:
        """Wrap ``h.store.apply`` so the first matching batch waits for ``open``."""
        self.reached = threading.Event()
        self._open = threading.Event()
        self._used = False
        real = h.store.apply

        def apply(batch: Batch) -> None:
            """Wait at the gate for the first matching batch, then commit."""
            if not self._used and matches(batch):
                self._used = True
                self.reached.set()
                self._open.wait(GATE_TIMEOUT)
            real(batch)

        monkeypatch.setattr(h.store, "apply", apply)

    def open(self) -> None:
        """Let the held commit go ahead."""
        self._open.set()

    async def wait_reached(self) -> None:
        """Wait (yielding to the loop) until the matching commit is being held."""
        while not self.reached.is_set():
            await pynet.loop.sleep(POLL)
