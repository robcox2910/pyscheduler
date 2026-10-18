"""Read and check the keys that make a job a file watcher.

A file watcher is a letterbox-watcher: it keeps looking at the box, and
only says "the parcel is here" once the parcel has stopped rustling. These
keys say which parcel (``watch``), how long it must be still (``min_age``),
how often to look (``poll``) and when to give up (``give_up_after``).
"""

import re
from datetime import timedelta

from pyscheduler.config.fields import Reader, read_duration, read_str
from pyscheduler.config.model import JobKind, WatchSpec
from pyscheduler.yamlish import Map, Node

DEFAULT_MIN_AGE = timedelta(seconds=30)
DEFAULT_POLL = timedelta(seconds=10)
DEFAULT_GIVE_UP_AFTER = timedelta(days=1)
WATCH_ONLY_KEYS = ("watch", "min_age", "poll", "give_up_after")
NOT_FOR_WATCHERS = ("command", "cwd", "env", "timeout", "pools", "stdout", "stderr")
NOT_IN_DEFAULTS = ("type", "watch")
_ZERO = re.compile(r"0[dhms]?")


def read_type(r: Reader, node: Node, what: str) -> JobKind | None:
    """Read ``type``: ``command`` or ``file_watcher``."""
    text = read_str(r, node, what)
    if text is None:
        return None
    try:
        return JobKind(text)
    except ValueError:
        kinds = ", ".join(kind.value for kind in JobKind)
        r.add(node.line, f"unknown job type {text!r}: use one of {kinds}")
        return None


def read_min_age(r: Reader, node: Node, what: str) -> timedelta | None:
    """Read ``min_age``: a duration, where ``0s`` means "stable on first sight"."""
    text = read_str(r, node, what)
    if text is not None and _ZERO.fullmatch(text):
        return timedelta(0)
    return read_duration(r, node, what)


def build_watch(
    pattern: str | None,
    min_age: timedelta | None,
    poll: timedelta | None,
    give_up_after: timedelta | None,
) -> WatchSpec | None:
    """Return the watch spec for a watcher, filling gaps with the defaults."""
    if pattern is None:
        return None
    return WatchSpec(
        pattern=pattern,
        min_age=DEFAULT_MIN_AGE if min_age is None else min_age,
        poll=poll or DEFAULT_POLL,
        give_up_after=give_up_after or DEFAULT_GIVE_UP_AFTER,
    )


def check_kind_keys(
    r: Reader, line: int, kind: JobKind | None, mapping: Map, *, has_command: bool
) -> None:
    """Note keys a job's kind forbids, and required keys it lacks.

    Only keys the job itself writes are judged: a ``defaults:`` section may
    hold a ``timeout`` for the command jobs, and watchers simply ignore it.
    ``kind`` is ``None`` when ``type`` was written wrongly (already noted).
    """
    items = mapping.items
    if "type" in items and kind is None:
        return
    if kind is JobKind.FILE_WATCHER:
        if "watch" not in items:
            r.add(line, "missing required key 'watch'")
        for key in NOT_FOR_WATCHERS:
            if key in items:
                r.add(mapping.key_lines[key], f"file watchers take no {key}")
        return
    if not has_command:
        r.add(line, "missing required key 'command'")
    for key in WATCH_ONLY_KEYS:
        if key in items:
            r.add(mapping.key_lines[key], f"{key} is only for file watchers")
