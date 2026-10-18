"""Check the ``{blanks}`` in every template of a job.

Like proof-reading a form letter before it is posted: every blank must be
one the scheduler knows how to fill. ``{files}`` is filled only for jobs
that wait on a file watcher, and a watcher's own ``watch`` pattern is filled
before any attempt or file exists.
"""

from collections.abc import Iterator
from typing import Protocol

from pyscheduler.config.model import Config, JobDef, JobKind, SourceRef
from pyscheduler.templates import PLACEHOLDERS, WATCH_PLACEHOLDERS, placeholders_in


class Checker(Protocol):
    """What the template checks need from the validator: a place to note problems."""

    def add(self, where: SourceRef, message: str, job: str | None) -> None:
        """Note one error at ``where``."""
        ...


def template_fields(job: JobDef) -> Iterator[tuple[str, str]]:
    """Yield ``(where, text)`` for every templated value of ``job``."""
    if job.watch is not None:
        yield "watch", job.watch.pattern
        return
    if isinstance(job.command, str):
        yield "command", job.command
    else:
        for arg in job.command:
            yield "command", arg
    if job.cwd is not None:
        yield "cwd", job.cwd
    for key, value in job.env.items():
        yield f"env {key}", value
    yield "stdout", job.stdout
    yield "stderr", job.stderr


def allowed_placeholders(config: Config, job: JobDef) -> frozenset[str]:
    """Return the blanks ``job`` may use; ``files`` needs a direct file-watcher upstream."""
    if job.kind is JobKind.FILE_WATCHER:
        return WATCH_PLACEHOLDERS
    for dep in job.depends_on:
        upstream = config.jobs.get(dep.job)
        if upstream is not None and upstream.kind is JobKind.FILE_WATCHER:
            return PLACEHOLDERS
    return PLACEHOLDERS - {"files"}


def check_placeholders(checker: Checker, config: Config, job: JobDef) -> None:
    """Note malformed or unknown placeholders in every template of ``job``."""
    allowed = allowed_placeholders(config, job)
    choices = ", ".join(f"{{{n}}}" for n in sorted(allowed))
    reported: set[tuple[str, str]] = set()
    for where, text in template_fields(job):
        try:
            names = placeholders_in(text)
        except ValueError as exc:
            checker.add(job.source, f"{where}: {exc}", job.name)
            continue
        for name in sorted(names - allowed):
            if (where, name) not in reported:
                reported.add((where, name))
                message = f"unknown placeholder {{{name}}} in {where}: use one of {choices}"
                checker.add(job.source, message, job.name)
