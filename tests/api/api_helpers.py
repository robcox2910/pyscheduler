"""The API test harness: an engine on a fake clock behind a pyweb ``TestClient``."""

import hashlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import pynet.loop
from pyweb.testing import TestClient, TestResponse

from pyscheduler.config import Config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher

START = datetime(2026, 10, 19, tzinfo=UTC)
"""Where every harness clock starts: midnight UTC on Monday 19 October 2026."""

TODAY = date(2026, 10, 19)
AT_1830 = START.replace(hour=18, minute=30)
AFTER_CHAIN = AT_1830 + timedelta(minutes=5)
"""When ``extract`` (18:30, one minute) and then ``load`` have both succeeded."""

MAX_STEPS = 10_000

VIEWER_KEY = "viewer-secret"
OPERATOR_KEY = "operator-secret"
VIEWER = {"Authorization": f"Bearer {VIEWER_KEY}"}
OPERATOR = {"Authorization": f"Bearer {OPERATOR_KEY}"}

OK = 200
BAD_REQUEST = 400
UNAUTHORISED = 401
FORBIDDEN = 403
NOT_FOUND = 404
CONFLICT = 409

JOBS = """
jobs:
  extract:
    command: run-extract
    schedule: "30 18 * * *"
    description: Pull the trades
    owner: ops
    tags: [finance]
    criticality: high
  load:
    command: run-load
    depends_on: [extract]
  adhoc:
    command: run-adhoc
    schedule: manual
"""

EXTRACT_RUN = "extract.2026-10-19.1"
LOAD_RUN = "load.2026-10-19.1"

type Body = Callable[[], Coroutine[Any, Any, None]]


def sha(token: str) -> str:
    """Return the lowercase hex SHA-256 of ``token``."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_yaml() -> str:
    """Return a tokens file with one viewer and one operator."""
    return (
        f"- {{name: vera, token_sha256: {sha(VIEWER_KEY)}, role: viewer}}\n"
        f"- {{name: otto, token_sha256: {sha(OPERATOR_KEY)}, role: operator}}\n"
    )


def body_of(response: TestResponse) -> dict[str, Any]:
    """Return the response's JSON object."""
    return cast("dict[str, Any]", response.json())


@dataclass
class ApiHarness:
    """Hold one engine, its fakes and a client for its API."""

    tmp_path: Path
    config_path: Path
    config: Config
    store: Store
    clock: FakeClock
    launcher: FakeLauncher
    engine: Engine
    client: TestClient

    async def advance_to(self, when: datetime) -> None:
        """Move the clock to ``when`` one timer at a time, settling the engine after each."""
        await self.engine.settle()
        for _ in range(MAX_STEPS):
            due = self.clock.next_due()
            if due is None or due > when:
                break
            await self.clock.advance_to(due)
            await self.engine.settle()
        await self.clock.advance_to(when)
        await self.engine.settle()

    def run(self, body: Body) -> None:
        """Run ``body`` inside ``pynet.loop.run`` between engine start and stop."""

        async def main() -> None:
            """Start the engine, run the body, stop the engine."""
            await self.engine.start()
            try:
                await self.engine.settle()
                await body()
            finally:
                await self.engine.stop()

        pynet.loop.run(main())


class MakeApi(Protocol):
    """Build an API harness from the YAML that follows ``scheduler:``."""

    def __call__(
        self, text: str = JOBS, *, tokens: bool = True, env: dict[str, str] | None = None
    ) -> ApiHarness:
        """Return a harness; ``tokens=False`` is development mode; ``env`` feeds ``${NAME}``."""
        ...
