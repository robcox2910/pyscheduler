"""Every ```python snippet in the concept pages and the index runs (unless marked no-run)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
"""The repository root: snippets run from here."""

PAGES = sorted([*(ROOT / "docs" / "concepts").glob("*.md"), ROOT / "docs" / "index.md"])
"""The pages whose snippets are checked."""

NO_RUN = "<!-- no-run -->"
"""Put this on the line before a snippet that cannot run on its own."""

SNIPPET_SECONDS = 8.0
"""How long one snippet may take."""

_FENCE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _snippets() -> list[tuple[str, str]]:
    """Return ``(id, code)`` for every runnable python snippet."""
    found: list[tuple[str, str]] = []
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for match in _FENCE.finditer(text):
            before = text[: match.start()].rstrip("\n").rsplit("\n", 1)[-1]
            if before.strip() == NO_RUN:
                continue
            line = text.count("\n", 0, match.start()) + 1
            found.append((f"{page.relative_to(ROOT)}:{line}", match.group(1)))
    return found


SNIPPETS = _snippets()


@pytest.mark.timeout(SNIPPET_SECONDS + 2)
@pytest.mark.parametrize("code", [code for _, code in SNIPPETS], ids=[i for i, _ in SNIPPETS])
def test_snippet_runs(code: str, tmp_path: Path) -> None:
    """Run the snippet in a fresh interpreter from the repository root."""
    env = {**os.environ, "HOME": str(tmp_path)}
    done = subprocess.run(  # noqa: S603 -- our own documentation
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=SNIPPET_SECONDS,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_some_snippets_found() -> None:
    """The finder sees the pages' snippets (a broken pattern would pass vacuously)."""
    assert len(SNIPPETS) > len(PAGES) // 2
