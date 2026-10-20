"""The API test harness: an engine on a fake clock behind a pyweb ``TestClient``."""

import hashlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import pynet.loop
from pyweb.auth import InMemoryUsers, SessionAuth, User
from pyweb.messages import HttpResponse
from pyweb.security import SessionSigner, hash_password
from pyweb.testing import TestClient, TestResponse

from pyscheduler.api.access import SESSION_COOKIE
from pyscheduler.api.session_key import load_or_make_key
from pyscheduler.config import Config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.model import Attempt, Run, RunState, Trigger
from pyscheduler.fake_hub import FakeHub
from pyscheduler.store import Batch, Store
from pyscheduler.testing import FakeLauncher
from pyscheduler.ui.live import LiveFeed

START = datetime(2026, 10, 19, tzinfo=UTC)
"""Where every harness clock starts: midnight UTC on Monday 19 October 2026."""

TODAY = date(2026, 10, 19)
BOOT = "boot"
"""The boot id of a harness's live feed, so event ids read ``boot-1``, ``boot-2``..."""
AT_1830 = START.replace(hour=18, minute=30)
AFTER_CHAIN = AT_1830 + timedelta(minutes=5)
"""When ``extract`` (18:30, one minute) and then ``load`` have both succeeded."""

MAX_STEPS = 10_000

VIEWER_KEY = "viewer-secret"
OPERATOR_KEY = "operator-secret"
VIEWER = {"Authorization": f"Bearer {VIEWER_KEY}"}
OPERATOR = {"Authorization": f"Bearer {OPERATOR_KEY}", "Content-Type": "application/json"}
"""The operator's token, and the JSON type every POST must carry (ruling R24)."""
DEV = {"Host": "127.0.0.1:8470", "Content-Type": "application/json"}
"""What a development-mode caller sends: a loopback host and the JSON type."""

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


TEST_ROUNDS = 1
"""PBKDF2 rounds for test passwords: one round keeps the tests fast."""

PHRASE = "correct horse battery staple"
"""Every test user's password."""


def users_yaml(**roles: str) -> str:
    """Return a users file with one entry per ``name=role``, all with ``PHRASE``."""
    digest = hash_password(PHRASE, rounds=TEST_ROUNDS)
    return "".join(
        f"- {{name: {name}, password_hash: {digest}, role: {role}}}\n"
        for name, role in roles.items()
    )


def log_in(h: ApiHarness, name: str) -> str:
    """Give the harness's client a session cookie for ``name``; return its CSRF token.

    The cookie is signed with the key the app uses (``state_dir/session.key``),
    just as the login page will sign it.
    """
    key = load_or_make_key(h.config.settings.state_dir)
    auth = SessionAuth(SessionSigner(key), InMemoryUsers(), cookie_name=SESSION_COOKIE)
    response = HttpResponse()
    csrf = auth.login(response, User(name, ""))
    pair = (response.headers.get("Set-Cookie") or "").partition(";")[0]
    cookie, _, value = pair.partition("=")
    h.client.cookies[cookie] = value
    return csrf


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
    live: LiveFeed | None = None

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
        self,
        text: str = JOBS,
        *,
        tokens: bool = True,
        env: dict[str, str] | None = None,
        users: str | None = None,
        live: bool = False,
    ) -> ApiHarness:
        """Return a harness; ``tokens=False`` is development mode; ``env`` feeds ``${NAME}``.

        ``users`` is the text of a users file (normal mode, with web sessions).
        ``live`` attaches a ``LiveFeed`` (boot id ``BOOT``) and serves ``/events``.
        """
        ...


REMOTE_AGENT = "build-01"
"""The agent the remote-log tests run attempts on."""
AGENT_LOG = "/agent/logs/adhoc/2026-10-19/adhoc.2026-10-19.1.out"
"""Where that agent says it wrote the stdout of ``ADHOC_RUN``."""
ADHOC_RUN = "adhoc.2026-10-19.1"


def remote_attempt(h: ApiHarness, *, ended: bool, paths: bool = True) -> FakeHub:
    """Give the harness's engine a ``FakeHub`` with ``REMOTE_AGENT`` online and a remote attempt.

    The store gets a run of ``adhoc`` with attempt 1 on that agent (finished when ``ended``).
    Without ``paths`` the agent has not reported ``started`` yet. The hub's stdout log
    for it holds ``one`` and ``two`` as whole lines.
    """
    hub = FakeHub(h.clock)
    hub.connect(REMOTE_AGENT)
    hub.logs[ADHOC_RUN, 1, "stdout"] = b"one\ntwo\n"
    h.engine.placement = hub
    run = Run(
        ADHOC_RUN,
        "adhoc",
        TODAY,
        START,
        RunState.RUNNING,
        "",
        1,
        0,
        Trigger.MANUAL,
        1,
        START,
        START,
    )
    row = Attempt(
        run_id=run.run_id,
        attempt=1,
        started_at=START,
        ended_at=START + timedelta(minutes=1) if ended else None,
        exit_code=0 if ended else None,
        signal=None,
        outcome=None,
        pid=None,
        stdout_path=AGENT_LOG if paths else "",
        stderr_path=AGENT_LOG.replace(".out", ".err") if paths else "",
        status_path=f"agent:{REMOTE_AGENT}",
        reason="",
    )
    h.store.apply(Batch(runs=[run], attempts=[row]))
    return hub
