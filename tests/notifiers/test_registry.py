"""Tests for the notifier registry and the option checks."""

import importlib.metadata
import textwrap
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pynet.loop
import pytest
from pynet.messaging import JsonValue

from pyscheduler.config import ConfigError, Settings, load_config
from pyscheduler.config.model import NotifierDef
from pyscheduler.notifiers import Notifier, NotifierFactory, build_notifier, check_notifier


def write(tmp_path: Path, notifiers: str) -> Path:
    """Write a config with the given ``notifiers:`` body and return its path."""
    text = "notifiers:\n" + textwrap.indent(textwrap.dedent(notifiers), "  ")
    text += "jobs:\n  a: {command: 'true', schedule: manual}\n"
    path = tmp_path / "scheduler.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def definition_and_settings(
    tmp_path: Path, options: Mapping[str, object]
) -> tuple[NotifierDef, Settings]:
    """Return notifier ``n`` with ``options`` (type included) and the settings of a valid config."""
    config = load_config(write(tmp_path, "n: {type: log, path: a.log}\n"), env={})
    kind = str(options["type"])
    rest = {k: v for k, v in options.items() if k != "type"}
    definition = replace(config.notifiers["n"], type=kind, options=MappingProxyType(rest))
    return definition, config.settings


class Recorder:
    """A plug-in notifier that remembers what it was sent."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[Mapping[str, JsonValue]] = []

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Remember ``alert``."""
        self.sent.append(alert)


def make_recorder(definition: NotifierDef, settings: Settings) -> Notifier:
    """Build a recorder (a plug-in factory)."""
    del definition, settings
    return Recorder()


class FakeEntryPoint:
    """Stand-in for an entry point that loads to ``make_recorder``."""

    name = "recorder"

    def load(self) -> NotifierFactory:
        """Return the factory."""
        return make_recorder


def test_registry_unknown_options_and_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing and unknown options are reported; a plug-in type builds."""
    definition, settings = definition_and_settings(tmp_path, {"type": "log"})
    assert check_notifier(definition, settings) == [
        "missing required option 'path' for log notifier"
    ]
    definition, settings = definition_and_settings(
        tmp_path, {"type": "log", "path": "a.log", "colour": "red"}
    )
    assert check_notifier(definition, settings) == ["unknown option 'colour' for log notifier"]

    def fake(*, group: str) -> list[FakeEntryPoint]:
        """Return the fake entry point for our group."""
        assert group == "pyscheduler.notifiers"
        return [FakeEntryPoint()]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    definition, settings = definition_and_settings(tmp_path, {"type": "recorder"})
    assert check_notifier(definition, settings) == []
    notifier = build_notifier(definition, settings)
    pynet.loop.run(notifier.send({"a": 1}))
    assert isinstance(notifier, Recorder)
    assert notifier.sent == [{"a": 1}]


def test_option_types_are_checked(tmp_path: Path) -> None:
    """Options of the wrong shape are reported."""
    definition, settings = definition_and_settings(tmp_path, {"type": "log", "path": ["a"]})
    assert check_notifier(definition, settings) == ["option 'path' for log notifier must be text"]
    definition, settings = definition_and_settings(tmp_path, {"type": "command", "command": 5})
    assert "must be text or a list of text" in check_notifier(definition, settings)[0]


def test_unregistered_http_types_are_accepted(tmp_path: Path) -> None:
    """Types Task 4 registers pass for now, whatever their options."""
    definition, settings = definition_and_settings(tmp_path, {"type": "slack", "anything": 1})
    assert check_notifier(definition, settings) == []


def test_build_unknown_type_raises(tmp_path: Path) -> None:
    """Building a type nobody registered is a ValueError."""
    definition, settings = definition_and_settings(tmp_path, {"type": "nope"})
    with pytest.raises(ValueError, match="unknown notifier type"):
        build_notifier(definition, settings)


def test_validation_reports_option_errors(tmp_path: Path) -> None:
    """Loading a config whose log notifier lacks ``path`` fails at the notifier's line."""
    path = write(tmp_path, "n: {type: log}\n")
    with pytest.raises(ConfigError) as caught:
        load_config(path, env={})
    problems = caught.value.problems
    assert [(p.line, p.message) for p in problems] == [
        (2, "missing required option 'path' for log notifier")
    ]
