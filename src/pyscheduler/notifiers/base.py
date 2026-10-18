"""The notifier contract and the registry that turns a config entry into a notifier.

A notifier is one way to reach someone: the tannoy, a note home, a text.
Each knows how to deliver one alert and says so loudly (``NotifyError``)
when it cannot. The registry is the school office's address book: give it
a notifier's ``type`` and it finds the right kind of messenger.
"""

from collections.abc import Callable, Mapping
from typing import Protocol

from pynet.messaging import JsonValue

from pyscheduler.config.alert_fields import notifier_entry_points
from pyscheduler.config.model import NotifierDef, Settings


class NotifyError(Exception):
    """A send failed; its message is safe to store once it has been redacted."""


class Notifier(Protocol):
    """Something that can deliver one alert."""

    async def send(self, alert: Mapping[str, JsonValue]) -> None:
        """Deliver ``alert``.

        Raises:
            NotifyError: If the alert could not be delivered.

        """
        ...


type NotifierFactory = Callable[[NotifierDef, Settings], Notifier]
"""Builds a notifier from its config; raises ``ValueError(message)`` for bad options.

A factory must not do I/O: validation calls it just to check the options.
"""

UNREGISTERED_TYPES = frozenset({"webhook", "slack", "telegram", "email"})
"""Built-in types whose factories are not written yet: accepted as valid, never built.

Task 4 registers these in ``builtin_factories`` and empties this set.
"""


def builtin_factories() -> dict[str, NotifierFactory]:
    """Return the built-in notifier factories by type name."""
    from pyscheduler.notifiers.local import CommandNotifier, LogNotifier  # noqa: PLC0415

    return {"log": LogNotifier.from_config, "command": CommandNotifier.from_config}


def check_options(
    definition: NotifierDef, *, required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> None:
    """Raise ``ValueError`` if the options miss a required key or hold an unknown one."""
    kind = definition.type
    for key in required:
        if key not in definition.options:
            message = f"missing required option {key!r} for {kind} notifier"
            raise ValueError(message)
    for key in definition.options:
        if key not in required and key not in optional:
            message = f"unknown option {key!r} for {kind} notifier"
            raise ValueError(message)


def build_notifier(definition: NotifierDef, settings: Settings) -> Notifier:
    """Build the notifier for ``definition``: a built-in type, else a plug-in entry point.

    Raises:
        ValueError: If the type is unknown or the options are bad.

    """
    factory = builtin_factories().get(definition.type)
    if factory is None:
        entry = notifier_entry_points().get(definition.type)
        if entry is None:
            message = f"unknown notifier type {definition.type!r}"
            raise ValueError(message)
        factory = entry.load()
    return factory(definition, settings)


def check_notifier(definition: NotifierDef, settings: Settings) -> list[str]:
    """Return the problems with ``definition``'s options (empty when it is fine)."""
    if definition.type in UNREGISTERED_TYPES:
        return []
    try:
        build_notifier(definition, settings)
    except ValueError as exc:
        return [str(exc)]
    return []


__all__ = [
    "UNREGISTERED_TYPES",
    "Notifier",
    "NotifierFactory",
    "NotifyError",
    "build_notifier",
    "builtin_factories",
    "check_notifier",
    "check_options",
]
