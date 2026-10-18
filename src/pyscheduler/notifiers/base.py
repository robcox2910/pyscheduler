"""The notifier contract and the registry that turns a config entry into a notifier.

A notifier is one way to reach someone: the tannoy, a note home, a text.
Each knows how to deliver one alert and says so loudly (``NotifyError``)
when it cannot. The registry is the school office's address book: give it
a notifier's ``type`` and it finds the right kind of messenger.
"""

from collections.abc import Callable, Mapping
from typing import Protocol, cast

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


def builtin_factories() -> dict[str, NotifierFactory]:
    """Return the built-in notifier factories by type name."""
    from pyscheduler.notifiers.email import EmailNotifier  # noqa: PLC0415
    from pyscheduler.notifiers.local import CommandNotifier, LogNotifier  # noqa: PLC0415
    from pyscheduler.notifiers.web import (  # noqa: PLC0415
        SlackNotifier,
        TelegramNotifier,
        WebhookNotifier,
    )

    return {
        "log": LogNotifier.from_config,
        "command": CommandNotifier.from_config,
        "webhook": WebhookNotifier.from_config,
        "slack": SlackNotifier.from_config,
        "telegram": TelegramNotifier.from_config,
        "email": EmailNotifier.from_config,
    }


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


def text_option(definition: NotifierDef, key: str, default: str | None = None) -> str:
    """Return option ``key`` as non-empty text (or ``default`` when absent).

    Raises:
        ValueError: If the option is missing with no default, or is not text.

    """
    raw = definition.options.get(key, default)
    if not isinstance(raw, str) or not raw:
        message = f"option {key!r} for {definition.type} notifier must be text"
        raise ValueError(message)
    return raw


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
        try:
            loaded = entry.load()
        except Exception as exc:
            message = f"cannot load notifier type {definition.type!r}: {exc}"
            raise ValueError(message) from exc
        if not callable(loaded):
            message = f"cannot load notifier type {definition.type!r}: not a callable factory"
            raise ValueError(message)
        factory = cast("NotifierFactory", loaded)
    return factory(definition, settings)


def check_notifier(definition: NotifierDef, settings: Settings) -> list[str]:
    """Return the problems with ``definition``'s options (empty when it is fine)."""
    try:
        build_notifier(definition, settings)
    except ValueError as exc:
        return [str(exc)]
    return []


__all__ = [
    "Notifier",
    "NotifierFactory",
    "NotifyError",
    "build_notifier",
    "builtin_factories",
    "check_notifier",
    "check_options",
    "text_option",
]
