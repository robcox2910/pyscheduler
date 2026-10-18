"""Check the alert config as a whole, so every name it uses points at something real.

Like checking that every line on the school's phone tree has a real phone
at the end of it. Reading (``alert_fields``) only makes sure each piece is
well formed; here the pieces are laid side by side.
"""

from typing import Protocol

from pyscheduler.config.alert_fields import notifier_types
from pyscheduler.config.model import Config, SourceRef


class Checker(Protocol):
    """What the alert checks need from the validator: a place to note problems."""

    broken: frozenset[str]

    def add(self, where: SourceRef, message: str, job: str | None) -> None:
        """Note one error at ``where``."""
        ...


def check_alerts(checker: Checker, config: Config) -> None:
    """Note unknown notifier types and unknown notifier names in routes and jobs."""
    known = notifier_types()
    for notifier in config.notifiers.values():
        if notifier.type and notifier.type not in known:
            choices = ", ".join(sorted(known))
            checker.add(
                notifier.source,
                f"unknown notifier type {notifier.type!r}: use one of {choices}",
                None,
            )
    for route in config.routes:
        for name in route.notify:
            if name not in config.notifiers:
                checker.add(route.source, f"unknown notifier {name!r}", None)
    for job in config.jobs.values():
        if job.name in checker.broken:
            continue
        for name in job.alerts.notify:
            if name not in config.notifiers:
                checker.add(job.source, f"unknown notifier {name!r}", job.name)


__all__ = ["check_alerts"]
