"""Read the alert parts of a config: ``notifiers:``, ``routes:`` and a job's ``alerts:``.

Think of the school office. A notifier is one way to reach someone (the
tannoy, a note home, a text). A route is a rule on the wall: "anything
about the science lab goes on the tannoy". A job's ``alerts`` says which
bells that job rings. This module only reads and shapes these; checking
that the names point at something real happens later, in
``validate_alerts``.
"""

import importlib.metadata
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import cast

from pymq.broker.errors import InvalidTopicError
from pymq.broker.topics import validate_pattern

from pyscheduler.config.fields import (
    Reader,
    check_keys,
    read_duration,
    read_int,
    read_list,
    read_map,
    read_plain,
    read_str,
)
from pyscheduler.config.model import (
    AlertKind,
    JobAlerts,
    NotifierDef,
    NotifyRetry,
    Route,
    SourceRef,
)
from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node

NOTIFIER_TYPES = frozenset({"log", "command", "webhook", "slack", "telegram", "email"})
"""The notifier types built in; plug-ins add more (see ``notifier_types``)."""
ENTRY_POINT_GROUP = "pyscheduler.notifiers"
NOTIFIER_RESERVED = ("type", "retries", "timeout")
ROUTE_KEYS = ("match", "notify")
RETRY_KEYS = ("count", "delay", "max_delay")
ALERT_KEYS = ("on", "off", "notify")
ALERT_TOPIC_ROOT = "alert"
ALERT_TOPIC_WORDS = 4
WILDCARDS = frozenset({"*", "#"})
_DEFAULT_RETRY = NotifyRetry()
DEFAULT_NOTIFIER_TIMEOUT = timedelta(seconds=10)


def notifier_types() -> frozenset[str]:
    """Return every known notifier type: the built-ins plus plug-in entry points.

    The entry points are looked up on each call, so a plug-in installed
    (or, in tests, faked) later is seen by the next validation.
    """
    found = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    return NOTIFIER_TYPES | {entry.name for entry in found}


def _read_retries(r: Reader, node: Node, what: str) -> NotifyRetry:
    """Read a notifier's ``retries:`` mapping over the defaults."""
    mapping = read_map(r, node, what)
    if mapping is None:
        return _DEFAULT_RETRY
    check_keys(r, mapping, RETRY_KEYS, what)
    items = mapping.items
    count = read_int(r, items["count"], f"{what}.count", minimum=0) if "count" in items else None
    delay = read_duration(r, items["delay"], f"{what}.delay") if "delay" in items else None
    cap = (
        read_duration(r, items["max_delay"], f"{what}.max_delay") if "max_delay" in items else None
    )
    return NotifyRetry(
        count=_DEFAULT_RETRY.count if count is None else count,
        delay=delay or _DEFAULT_RETRY.delay,
        max_delay=cap or _DEFAULT_RETRY.max_delay,
    )


def _read_notifier(r: Reader, name: str, node: Node, line: int) -> NotifierDef | None:
    """Read one notifier: ``type`` (required), ``retries``, ``timeout`` and its own options."""
    what = f"notifier {name}"
    mapping = read_map(r, node, what)
    if mapping is None:
        return None
    items = mapping.items
    kind = read_str(r, items["type"], f"{what} type") if "type" in items else None
    if "type" not in items:
        r.add(line, "missing required key 'type'")
    retries = _read_retries(r, items["retries"], f"{what} retries") if "retries" in items else None
    timeout = read_duration(r, items["timeout"], f"{what} timeout") if "timeout" in items else None
    options: dict[str, object] = {}
    for key, child in items.items():
        if key not in NOTIFIER_RESERVED:
            value = read_plain(r, child, f"{what} {key}")
            if value is not None:
                options[key] = value
    return NotifierDef(
        name=name,
        type=kind or "",
        options=MappingProxyType(options),
        retries=retries or _DEFAULT_RETRY,
        timeout=timeout or DEFAULT_NOTIFIER_TIMEOUT,
        source=SourceRef(r.file, line),
    )


def read_notifiers(r: Reader, node: Map) -> dict[str, NotifierDef]:
    """Read the ``notifiers:`` mapping (names are checked; types are checked later)."""
    out: dict[str, NotifierDef] = {}
    for name, child in node.items.items():
        line = node.key_lines[name]
        try:
            check_name(name)
        except ValueError as exc:
            r.add(line, str(exc))
        notifier = _read_notifier(r, name, child, line)
        if notifier is not None:
            out[name] = notifier
    return out


def _can_match(pattern: str) -> bool:
    """Return whether ``pattern`` could match some ``alert.<kind>.<criticality>.<job>`` topic."""
    words = pattern.split(".")
    if words[0] not in WILDCARDS | {ALERT_TOPIC_ROOT}:
        return False
    return "#" in words or len(words) == ALERT_TOPIC_WORDS


def _read_route(r: Reader, node: Node, _what: str) -> Route | None:
    """Read one route: a topic pattern and the notifiers it sends to."""
    mapping = read_map(r, node, "route")
    if mapping is None:
        return None
    items = mapping.items
    suggested = check_keys(r, mapping, ROUTE_KEYS, "route")
    for key in ROUTE_KEYS:
        if key not in items and key not in suggested:
            r.add(node.line, f"missing required key {key!r}")
    match = read_str(r, items["match"], "route match") if "match" in items else None
    notify = (
        read_list(r, items["notify"], "route notify", _read_name) if "notify" in items else None
    )
    if match is None or notify is None:
        return None
    try:
        validate_pattern(match)
    except InvalidTopicError:
        r.add(items["match"].line, f"invalid route pattern {match!r}")
        return None
    if not _can_match(match):
        r.add(
            items["match"].line,
            f"route pattern {match!r} never matches an alert topic "
            "(alerts are alert.<kind>.<criticality>.<job>)",
            "warning",
        )
    return Route(match, tuple(notify), SourceRef(r.file, node.line))


def _read_name(r: Reader, node: Node, what: str) -> str | None:
    """Read one list item as text."""
    return read_str(r, node, what)


def read_routes(r: Reader, node: Node) -> tuple[Route, ...]:
    """Read the ``routes:`` list."""
    routes = read_list(r, node, "routes", _read_route)
    return () if routes is None else tuple(routes)


def read_alert_sections(
    r: Reader, main: Mapping[str, Node]
) -> tuple[dict[str, NotifierDef], tuple[Route, ...]]:
    """Read ``notifiers:`` and ``routes:`` from the main file's top-level ``main`` items."""
    notifiers: dict[str, NotifierDef] = {}
    if "notifiers" in main:
        section = read_map(r, main["notifiers"], "notifiers")
        notifiers = {} if section is None else read_notifiers(r, section)
    routes = read_routes(r, main["routes"]) if "routes" in main else ()
    return notifiers, routes


def _kinds(r: Reader, raw: object, what: str, line: int) -> frozenset[AlertKind]:
    """Turn a raw list of kind names into alert kinds, noting each bad one."""
    out: set[AlertKind] = set()
    for name in _strings(r, raw, what, line):
        try:
            out.add(AlertKind(name))
        except ValueError:
            choices = ", ".join(k.value for k in AlertKind)
            r.add(line, f"unknown alert kind {name!r}: use one of {choices}")
    return frozenset(out)


def _strings(r: Reader, raw: object, what: str, line: int) -> list[str]:
    """Return the text items of a raw list, noting a problem if it is not a list."""
    if not isinstance(raw, list):
        r.add(line, f"{what} must be a list")
        return []
    out: list[str] = []
    for item in cast("list[object]", raw):
        if isinstance(item, str):
            out.append(item)
        else:
            r.add(line, f"{what} items must be text")
    return out


def to_job_alerts(r: Reader, raw: Mapping[str, object] | None, line: int) -> JobAlerts:
    """Turn a job's merged raw ``alerts`` mapping into ``JobAlerts``.

    Problems are noted at ``line`` (the job's line), since the merged
    mapping no longer knows where each key was written.
    """
    if not raw:
        return JobAlerts()
    for key in raw:
        if key not in ALERT_KEYS:
            r.add(line, f"unknown key {key!r} in alerts: use one of {', '.join(ALERT_KEYS)}")
    notify = _strings(r, raw["notify"], "alerts.notify", line) if "notify" in raw else []
    return JobAlerts(
        on=_kinds(r, raw["on"], "alerts.on", line) if "on" in raw else frozenset(),
        off=_kinds(r, raw["off"], "alerts.off", line) if "off" in raw else frozenset(),
        notify=tuple(notify),
    )


__all__ = [
    "NOTIFIER_TYPES",
    "notifier_types",
    "read_alert_sections",
    "read_notifiers",
    "read_routes",
    "to_job_alerts",
]
