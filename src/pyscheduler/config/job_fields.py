"""Read a job's keys (and the ``defaults:`` section) into a ``JobDef``.

Reading happens in two steps. First each mapping (the defaults, then each
job) is read into a ``JobFields``, where every key is optional. Then the
job's fields are laid over the defaults, like a transparent sheet over a
form: wherever the job wrote something it shows; elsewhere the default
shows through. Finally the gaps are filled with the built-in defaults.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo

from pyscheduler.config.alert_fields import to_job_alerts
from pyscheduler.config.fields import (
    Reader,
    check_keys,
    read_bool,
    read_duration,
    read_enum,
    read_float,
    read_int,
    read_list,
    read_map,
    read_must_start_by,
    read_plain,
    read_str,
    read_str_list,
    read_str_map,
    read_template,
    read_zone,
)
from pyscheduler.config.model import (
    CatchUp,
    Criticality,
    Dependency,
    DepOn,
    JobDef,
    JobKind,
    MustStartBy,
    Overlap,
    RetryPolicy,
    RunDateRule,
    SourceRef,
)
from pyscheduler.config.schedule_fields import read_schedule
from pyscheduler.config.watch_fields import (
    NOT_IN_DEFAULTS,
    build_watch,
    read_min_age,
    read_type,
)
from pyscheduler.schedule.specs import Schedule
from pyscheduler.yamlish import Map, Node, Scalar

JOB_KEYS = (
    "command", "cwd", "env", "schedule", "calendar", "timezone", "run_date", "depends_on",
    "retries", "timeout", "criticality", "pools", "overlap", "catch_up", "stdout", "stderr",
    "min_runtime", "max_runtime", "must_start_by", "alerts", "agent", "agent_tags", "tags",
    "description", "owner", "enabled", "type", "watch", "min_age", "poll", "give_up_after",
)  # fmt: skip
RETRY_KEYS = ("count", "delay", "backoff", "max_delay")
DEPENDENCY_KEYS = ("job", "on", "run_date")
NO_CALENDAR = "none"
DEFAULT_STDOUT = "{log_dir}/{job}/{run_date}/{run_id}.{attempt}.out"
DEFAULT_STDERR = "{log_dir}/{job}/{run_date}/{run_id}.{attempt}.err"


@dataclass
class RetryFields:
    """The retry keys that were written; ``None`` means not given."""

    count: int | None = None
    delay: timedelta | None = None
    backoff: float | None = None
    max_delay: timedelta | None = None


@dataclass
class JobFields:
    """The job keys that were written; ``None`` means not given.

    ``calendar`` keeps the text ``none`` so that a job can override a
    default calendar with "no calendar".
    """

    command: str | tuple[str, ...] | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    schedule: tuple[tuple[Schedule, ...], bool] | None = None
    calendar: str | None = None
    timezone: ZoneInfo | None = None
    run_date: RunDateRule | None = None
    depends_on: tuple[Dependency, ...] | None = None
    retries: RetryFields | None = None
    timeout: timedelta | None = None
    criticality: Criticality | None = None
    pools: dict[str, int] | None = None
    overlap: Overlap | None = None
    catch_up: CatchUp | None = None
    stdout: str | None = None
    stderr: str | None = None
    min_runtime: timedelta | None = None
    max_runtime: timedelta | None = None
    must_start_by: MustStartBy | None = None
    alerts: dict[str, object] | None = None
    agent: str | None = None
    agent_tags: tuple[str, ...] | None = None
    tags: tuple[str, ...] | None = None
    description: str | None = None
    owner: str | None = None
    enabled: bool | None = None
    type: JobKind | None = None
    watch: str | None = None
    min_age: timedelta | None = None
    poll: timedelta | None = None
    give_up_after: timedelta | None = None


def _read_command(r: Reader, node: Node) -> str | tuple[str, ...] | None:
    """Read ``command``: a shell string or a non-empty list of arguments."""
    if isinstance(node, Scalar):
        text = read_template(r, node, "command")
        if text is not None and not text.strip():
            r.add(node.line, "command must not be empty")
            return None
        return text
    args = read_str_list(r, node, "command", template=True)
    if args is not None and not args:
        r.add(node.line, "command must not be an empty list")
        return None
    return args


def _read_criticality(r: Reader, node: Node) -> Criticality | None:
    """Read ``criticality`` by name."""
    text = read_str(r, node, "criticality")
    if text is None:
        return None
    try:
        return Criticality.from_text(text)
    except ValueError as exc:
        r.add(node.line, str(exc))
        return None


def _read_retries(r: Reader, node: Node) -> RetryFields | None:
    """Read ``retries: {count, delay, backoff, max_delay}``."""
    mapping = read_map(r, node, "retries")
    if mapping is None:
        return None
    check_keys(r, mapping, RETRY_KEYS, "retries")
    items = mapping.items
    out = RetryFields()
    if "count" in items:
        out.count = read_int(r, items["count"], "retries.count")
    if "delay" in items:
        out.delay = read_duration(r, items["delay"], "retries.delay")
    if "backoff" in items:
        out.backoff = read_float(r, items["backoff"], "retries.backoff")
    if "max_delay" in items:
        out.max_delay = read_duration(r, items["max_delay"], "retries.max_delay")
    return out


def _read_dependency(r: Reader, node: Node, what: str) -> Dependency | None:
    """Read one dependency: a job name, or ``{job, on, run_date}``."""
    source = SourceRef(r.file, node.line)
    if not isinstance(node, Map):
        name = read_str(r, node, what)
        return None if name is None else Dependency(name, source=source)
    check_keys(r, node, DEPENDENCY_KEYS, "dependency")
    items = node.items
    if "job" not in items:
        r.add(node.line, "dependency needs job, for example {job: extract-trades}")
        return None
    name = read_str(r, items["job"], "dependency job")
    on = read_enum(r, items["on"], "dependency on", DepOn) if "on" in items else DepOn.SUCCESS
    offset = read_int(r, items["run_date"], "dependency run_date") if "run_date" in items else 0
    if name is None or on is None or offset is None:
        return None
    return Dependency(name, on, offset, source=source)


def _read_pools(r: Reader, node: Node) -> dict[str, int] | None:
    """Read ``pools: {name: slots}`` with at least one slot each."""
    mapping = read_map(r, node, "pools")
    if mapping is None:
        return None
    out: dict[str, int] = {}
    for key, child in mapping.items.items():
        slots = read_int(r, child, f"pools.{key}", minimum=1)
        if slots is not None:
            out[key] = slots
    return out


def _read_alerts(r: Reader, node: Node) -> dict[str, object] | None:
    """Read ``alerts`` as plain values (turned into ``JobAlerts`` by ``build_job``)."""
    if read_map(r, node, "alerts") is None:
        return None
    return cast("dict[str, object]", read_plain(r, node, "alerts"))


def _read_dependencies(r: Reader, node: Node, what: str) -> tuple[Dependency, ...] | None:
    """Read the ``depends_on`` list."""
    deps = read_list(r, node, what, _read_dependency)
    return None if deps is None else tuple(deps)


type KeyReader = Callable[[Reader, Node, str], object]


def _enum(enum: type[StrEnum]) -> KeyReader:
    """Return a key reader for a string enum."""
    return lambda r, node, what: read_enum(r, node, what, enum)


def _ignore_what(read: Callable[[Reader, Node], object]) -> KeyReader:
    """Adapt a reader that names its own key."""
    return lambda r, node, _what: read(r, node)


_KEY_READERS: dict[str, KeyReader] = {
    "command": _ignore_what(_read_command),
    "env": lambda r, node, what: read_str_map(r, node, what, template=True),
    "schedule": _ignore_what(read_schedule),
    "timezone": read_zone,
    "run_date": _enum(RunDateRule),
    "depends_on": _read_dependencies,
    "retries": _ignore_what(_read_retries),
    "criticality": _ignore_what(_read_criticality),
    "pools": _ignore_what(_read_pools),
    "overlap": _enum(Overlap),
    "catch_up": _enum(CatchUp),
    "must_start_by": read_must_start_by,
    "alerts": _ignore_what(_read_alerts),
    "enabled": read_bool,
    "type": read_type,
    "min_age": read_min_age,
    **dict.fromkeys(("poll", "give_up_after"), read_duration),
    **dict.fromkeys(("timeout", "min_runtime", "max_runtime"), read_duration),
    **dict.fromkeys(("agent_tags", "tags"), read_str_list),
    **dict.fromkeys(("cwd", "stdout", "stderr", "watch"), read_template),  # rendered per attempt
    **dict.fromkeys(("calendar", "agent", "description", "owner"), read_str),
}


def read_job_fields(r: Reader, node: Map, where: str) -> tuple[JobFields, set[str]]:
    """Read the keys of a job (or ``defaults:``) mapping.

    Returns:
        The fields, and the names suggested for misspelt keys.

    """
    suggested = check_keys(r, node, JOB_KEYS, where)
    out = JobFields()
    for key, child in node.items.items():
        if where == "defaults" and key in NOT_IN_DEFAULTS:
            r.add(node.key_lines[key], f"{key} is not allowed in defaults")
        elif key in JOB_KEYS:
            setattr(out, key, _KEY_READERS[key](r, child, key))
    return out, suggested


def _overlay[T: (JobFields, RetryFields)](base: T, top: T) -> T:
    """Return a copy of ``base`` with every field that ``top`` gives."""
    given = {f.name: v for f in fields(top) if (v := getattr(top, f.name)) is not None}
    return replace(base, **given)


def _deep_merge(base: Mapping[str, object], top: Mapping[str, object]) -> dict[str, object]:
    """Merge ``top`` over ``base`` key by key, recursing into nested mappings."""
    out = dict(base)
    for key, value in top.items():
        below = out.get(key)
        if isinstance(below, dict) and isinstance(value, dict):
            out[key] = _deep_merge(
                cast("dict[str, object]", below), cast("dict[str, object]", value)
            )
        else:
            out[key] = value
    return out


def merge_fields(defaults: JobFields, job: JobFields) -> JobFields:
    """Lay ``job`` over ``defaults``; nested mappings merge key by key.

    ``retries``, ``env`` and ``pools`` merge one level deep; ``alerts``
    merges at every level. Lists (such as ``tags``) are replaced whole.
    """
    merged = _overlay(defaults, job)
    if defaults.retries is not None and job.retries is not None:
        merged.retries = _overlay(defaults.retries, job.retries)
    if defaults.env is not None and job.env is not None:
        merged.env = {**defaults.env, **job.env}
    if defaults.pools is not None and job.pools is not None:
        merged.pools = {**defaults.pools, **job.pools}
    if defaults.alerts is not None and job.alerts is not None:
        merged.alerts = _deep_merge(defaults.alerts, job.alerts)
    return merged


def _retry_policy(given: RetryFields | None) -> RetryPolicy:
    """Fill the gaps in the written retry keys with the defaults."""
    base = RetryPolicy()
    if given is None:
        return base
    return RetryPolicy(
        count=base.count if given.count is None else given.count,
        delay=given.delay or base.delay,
        backoff=base.backoff if given.backoff is None else given.backoff,
        max_delay=given.max_delay or base.max_delay,
    )


def build_job(r: Reader, name: str, f: JobFields, source: SourceRef, zone: ZoneInfo) -> JobDef:
    """Turn merged fields into a ``JobDef``, filling every gap with its default.

    ``zone`` is the scheduler's time zone, used when the job names none.
    ``r`` collects problems found in the merged ``alerts``.
    """
    schedules, manual = f.schedule or ((), False)
    watcher = f.type is JobKind.FILE_WATCHER
    calendar = None if f.calendar in {None, NO_CALENDAR} else f.calendar
    watch = build_watch(f.watch, f.min_age, f.poll, f.give_up_after) if watcher else None
    if watch is not None and watch.min_age > watch.give_up_after:
        r.add(source.line, "min_age must not be longer than give_up_after")
    return JobDef(
        name=name,
        command=() if watcher else f.command or "",
        cwd=None if watcher else f.cwd,
        env=MappingProxyType({} if watcher else f.env or {}),
        schedules=schedules,
        manual=manual,
        calendar=calendar,
        timezone=f.timezone or zone,
        run_date_rule=f.run_date or RunDateRule.SAME_DAY,
        depends_on=f.depends_on or (),
        retries=_retry_policy(f.retries),
        timeout=None if watcher else f.timeout,
        criticality=Criticality.NORMAL if f.criticality is None else f.criticality,
        pools=MappingProxyType({} if watcher or not f.pools else f.pools),
        overlap=f.overlap or Overlap.QUEUE,
        catch_up=f.catch_up or CatchUp.LATEST,
        stdout="" if watcher else DEFAULT_STDOUT if f.stdout is None else f.stdout,
        stderr="" if watcher else DEFAULT_STDERR if f.stderr is None else f.stderr,
        min_runtime=f.min_runtime,
        max_runtime=f.max_runtime,
        must_start_by=f.must_start_by,
        alerts=to_job_alerts(r, f.alerts, source.line),
        agent=f.agent,
        agent_tags=f.agent_tags or (),
        tags=f.tags or (),
        description=f.description or "",
        owner=f.owner or "",
        enabled=True if f.enabled is None else f.enabled,
        source=source,
        kind=JobKind.FILE_WATCHER if watcher else JobKind.COMMAND,
        watch=watch,
    )
