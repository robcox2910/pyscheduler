"""Show how one saved config differs from the one before it.

Think of two copies of a recipe book, one marked up with a red pen.
For every recipe (config file) that is in either copy, this module lays
the old and the new side by side, line by line: a ``-`` for a line that
was taken out, a ``+`` for a line put in, ``@@`` for "the next change is
here". Everything is shown as plain text, so a line that holds markup is
only ever displayed, never run.
"""

import json
from collections.abc import Mapping
from difflib import unified_diff
from pathlib import Path
from typing import cast

from pyscheduler.ui.html import Safe, h, join

__all__ = ["diff_lines", "diff_view", "sources_of"]

HEADER_LINES = 2
"""The ``---`` and ``+++`` lines that start each file's diff."""


def sources_of(sources_json: str) -> dict[str, str]:
    """Return the file texts (path to text) a stored config holds.

    Anything that is not a text is left out, so a damaged record shows less, not an error.
    """
    try:
        found: object = json.loads(sources_json)
    except ValueError:
        return {}
    if not isinstance(found, dict):
        return {}
    parsed = cast("dict[str, object]", found)
    return {str(name): text for name, text in parsed.items() if isinstance(text, str)}


def diff_lines(old: str, new: str, name: str, old_label: str, new_label: str) -> list[str]:
    """Return the unified diff of one file's two texts (no line endings; ``[]`` if same)."""
    return list(
        unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{name} ({old_label})",
            tofile=f"{name} ({new_label})",
            lineterm="",
        )
    )


def _line(position: int, text: str) -> Safe:
    """Return one diff line as a span (its class says what kind of line it is)."""
    if position < HEADER_LINES:
        kind = "diff-head"
    elif text.startswith("@@"):
        kind = "diff-hunk"
    elif text.startswith("+"):
        kind = "diff-add"
    elif text.startswith("-"):
        kind = "diff-del"
    else:
        kind = "diff-ctx"
    return h("span", {"class": kind}, text)


def diff_view(
    old: Mapping[str, str], new: Mapping[str, str], old_label: str, new_label: str
) -> Safe:
    """Return the diff of every file in either config, each in its own ``<pre>``.

    A file in only one of the two is shown as all added or all removed.
    Files are named by their base name only; one that did not change says so.
    """
    parts: list[Safe] = []
    for path in sorted(set(old) | set(new), key=lambda p: (Path(p).name, p)):
        name = Path(path).name
        lines = diff_lines(old.get(path, ""), new.get(path, ""), name, old_label, new_label)
        if lines:
            rows: list[Safe | str] = []
            for position, text in enumerate(lines):
                rows += [_line(position, text), "\n"]
            parts.append(h("pre", {"class": "diff", "data-file": name}, *rows))
        else:
            parts.append(h("p", {"class": "empty", "data-file": name}, f"{name}: unchanged"))
    return join(*parts) if parts else h("p", {"class": "empty"}, "No files in either version.")
