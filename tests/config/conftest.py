"""Shared helpers for config tests: write YAML files into ``tmp_path``."""

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

type WriteYaml = Callable[[str, str], Path]


@pytest.fixture
def write_yaml(tmp_path: Path) -> WriteYaml:
    """Return a helper that writes dedented YAML text to a file under ``tmp_path``."""

    def write(name: str, text: str) -> Path:
        """Write ``text`` (dedented) to ``tmp_path / name`` and return the path."""
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return path

    return write
