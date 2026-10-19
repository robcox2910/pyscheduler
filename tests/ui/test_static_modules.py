"""Test the page script's modules: they resolve, are served as JS, stay small, and reload when gone.

The page loads one module, ``/static/app.js``, which imports the others
from ``/static/`` too, so ``script-src 'self'`` covers them all. With
node on the path the modules are syntax-checked, and a small stand-in
browser (``js_harness.mjs``) checks that a closed live feed, a 401
fragment and a 401 action each reload the page (ruling FR1).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from api_helpers import MakeApi, users_yaml

from pyscheduler.ui import STATIC_DIR
from pyscheduler.ui.layout import TEMPLATES

OK = 200
MAX_LINES = 400
"""The repo's file-size rule, for JS as for Python."""
NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "js_harness.mjs"
NODE_SECONDS = 20.0
BACKOFF_FIRST_MS = 1000
IMPORT = re.compile(r"""^import\s+(?:[^"']*\sfrom\s+)?["'](?P<path>[^"']+)["'];$""", re.MULTILINE)
SCRIPT_TAG = re.compile(r"<script\b[^>]*>")
SESSION_EXPIRED = "Your session has expired — please sign in again"
LIVE_STOPPED = "Live updates stopped — reloading…"

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _modules() -> list[Path]:
    """Return every JS module shipped in the static folder."""
    return sorted(STATIC_DIR.glob("*.js"))


def _imports(path: Path) -> list[str]:
    """Return the module paths ``path`` imports."""
    return [m["path"] for m in IMPORT.finditer(path.read_text(encoding="utf-8"))]


def _node(*args: str) -> subprocess.CompletedProcess[str]:
    """Run node with ``args`` and return what happened."""
    assert NODE is not None
    return subprocess.run(  # noqa: S603 -- our own files, through the node on PATH
        [NODE, *args], capture_output=True, text=True, check=False, timeout=NODE_SECONDS
    )


class TestModules:
    """``app.js`` is one entry point; every module it reaches is there and small."""

    def test_one_module_entry(self) -> None:
        """The page shell loads exactly one script: the ``app.js`` module."""
        shell = (TEMPLATES / "page.html").read_text(encoding="utf-8")
        assert SCRIPT_TAG.findall(shell) == ['<script type="module" src="/static/app.js">']

    def test_imports_resolve_and_files_are_small(self) -> None:
        """Every import is ``./<file>.js`` next to it; every module is reached; none is long."""
        reached: set[str] = set()
        for module in _modules():
            for target in _imports(module):
                assert target.startswith("./"), target
                assert (STATIC_DIR / target).is_file(), target
                reached.add(target.removeprefix("./"))
            lines = module.read_text(encoding="utf-8").count("\n")
            assert lines <= MAX_LINES, f"{module.name} has {lines} lines"
        assert reached | {"app.js"} == {m.name for m in _modules()}

    def test_html_only_from_fragments(self) -> None:
        """``innerHTML`` is used once, for the server's own fragments in ``common.js``."""
        uses = {m.name: m.read_text(encoding="utf-8").count("innerHTML") for m in _modules()}
        assert {name: n for name, n in uses.items() if n} == {"common.js": 1}

    def test_modules_are_served(self, api: MakeApi) -> None:
        """Each module is served publicly as JavaScript (a module script needs that type)."""
        h_ = api(users=users_yaml(ada="admin"))

        async def body() -> None:
            """Fetch every module with no login."""
            for module in _modules():
                response = await h_.client.get(f"/static/{module.name}")
                assert response.status == OK, module.name
                assert "javascript" in (response.headers.get("Content-Type") or "")

        h_.run(body)


@needs_node
class TestWithNode:
    """With node: the modules parse, and a gone session reloads the page."""

    @pytest.mark.parametrize("name", [m.name for m in _modules()])
    def test_syntax(self, name: str) -> None:
        """``node --check`` accepts the module."""
        done = _node("--check", str(STATIC_DIR / name))
        assert done.returncode == 0, done.stderr

    @pytest.mark.parametrize(
        ("case", "notice"),
        [("closed", LIVE_STOPPED), ("fragment", SESSION_EXPIRED), ("post", SESSION_EXPIRED)],
    )
    def test_gone_session_reloads(self, case: str, notice: str) -> None:
        """A closed feed, a 401 fragment and a 401 action each show a line and reload once."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), case)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {"reloads": 1, "notice": notice}

    def test_restart_waits_for_the_scheduler(self) -> None:
        """A 503 from ``/api/health`` is waited out (1 s, then 2 s) before the one reload."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "restart")
        assert done.returncode == 0, done.stderr
        seen = json.loads(done.stdout)
        assert seen["reloads"] == 1
        assert seen["delays"][-2:] == [BACKOFF_FIRST_MS, BACKOFF_FIRST_MS * 2]
        assert seen["notice"] == "waiting"

    def test_restart_waits_out_a_network_error(self) -> None:
        """A network error from ``/api/health`` is waited out once, then the page reloads."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "restart-net")
        assert done.returncode == 0, done.stderr
        seen = json.loads(done.stdout)
        assert seen["reloads"] == 1
        assert seen["delays"][-1] == BACKOFF_FIRST_MS

    def test_board_moves_on_only_when_the_day_has_changed(self) -> None:
        """A later-dated run before midnight does not reload; after midnight it does, checked once a minute."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "board-early")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == [
            {"reloads": 0, "checks": 1},
            {"reloads": 0, "checks": 1},
            {"reloads": 1, "checks": 2},
        ]

    def test_board_has_one_health_check_in_flight(self) -> None:
        """Two events during one pending check cost one check."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "board-inflight")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == [
            {"reloads": 0, "checks": 1},
            {"reloads": 1, "checks": 1},
        ]

    def test_restart_poller_cuts_off_a_hung_check(self) -> None:
        """A health check that never answers is timed out, backed off from, and the page then reloads."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "restart-hang")
        assert done.returncode == 0, done.stderr
        seen = json.loads(done.stdout)
        assert seen["reloads"] == 1
        assert seen["delays"][-1] == BACKOFF_FIRST_MS

    def test_board_trailing_check_finds_midnight(self) -> None:
        """Events held back by the throttle leave one trailing check, which reloads once the day moved."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "board-trailing")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == [
            {"reloads": 0, "checks": 1},
            {"reloads": 0, "checks": 1},
            {"timers": 1},
            {"reloads": 1, "checks": 2},
        ]

    def test_board_hung_check_does_not_block_the_next(self) -> None:
        """A health check that hangs is cut off, so a later event can check again."""
        done = _node(str(HARNESS), str(STATIC_DIR / "common.js"), "board-hang")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == [{"reloads": 0, "checks": 2}]
