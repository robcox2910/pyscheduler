"""The config model: typed, read-only descriptions of jobs and settings.

Think of a recipe book that has been checked and copied out neatly. The
YAML file is the scribbled version; these classes are the clean copy that
the rest of the scheduler cooks from. Nothing here can be changed after it
is made, so many parts of the scheduler can share one config safely.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar
from pyscheduler.config.agent_secrets import AgentSecrets
from pyscheduler.schedule.firings import RunDateRule
from pyscheduler.schedule.specs import Schedule

__all__ = [
    "DEFAULT_OFF",
    "AgentsDef",
    "AlertKind",
    "CatchUp",
    "Config",
    "ConfigError",
    "Criticality",
    "DepOn",
    "Dependency",
    "JobAlerts",
    "JobDef",
    "JobKind",
    "MustStartBy",
    "NotifierDef",
    "NotifyRetry",
    "Overlap",
    "Problem",
    "RetryPolicy",
    "Route",
    "RunDateRule",
    "Settings",
    "SourceRef",
    "WatchSpec",
]

DEFAULT_RETRY_DELAY = timedelta(minutes=1)
DEFAULT_MAX_DELAY = timedelta(minutes=10)
DEFAULT_BACKOFF = 2.0

type MustStartBy = time | timedelta


class Criticality(IntEnum):
    """How much a job matters; higher numbers matter more."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3

    @classmethod
    def from_text(cls, text: str) -> Criticality:
        """Return the criticality named ``text`` (``low``, ``normal``, ``high``, ``critical``).

        Raises:
            ValueError: If the name is unknown.

        """
        try:
            return cls[text.upper()]
        except KeyError:
            names = ", ".join(c.name.lower() for c in cls)
            msg = f"unknown criticality {text!r}: use one of {names}"
            raise ValueError(msg) from None


class DepOn(StrEnum):
    """Which final state of the upstream run satisfies a dependency."""

    SUCCESS = "success"
    FAILURE = "failure"
    DONE = "done"


class Overlap(StrEnum):
    """What to do when a firing arrives while the previous run is still active."""

    QUEUE = "queue"
    SKIP = "skip"
    ALLOW = "allow"


class CatchUp(StrEnum):
    """Which firings missed while the scheduler was down are run."""

    LATEST = "latest"
    ALL = "all"
    NONE = "none"


class AlertKind(StrEnum):
    """The kinds of thing that can raise an alert."""

    FAILED = "failed"
    RETRYING = "retrying"
    NEVER_STARTED = "never_started"
    TOO_LONG = "too_long"
    TOO_SHORT = "too_short"
    LOST = "lost"
    BLOCKED = "blocked"
    RELOAD_FAILED = "reload_failed"
    WATCHER_TIMEOUT = "watcher_timeout"


DEFAULT_OFF: frozenset[AlertKind] = frozenset({AlertKind.RETRYING})
"""The kinds that stay quiet unless a job turns them on."""


class JobKind(StrEnum):
    """What a job does: run a command, or wait for files to appear."""

    COMMAND = "command"
    FILE_WATCHER = "file_watcher"


@dataclass(frozen=True)
class WatchSpec:
    """What a file watcher waits for: a pattern, how still it must be, and when to quit."""

    pattern: str
    min_age: timedelta
    poll: timedelta
    give_up_after: timedelta


@dataclass(frozen=True)
class SourceRef:
    """Where something was written: a file and a 1-based line."""

    file: str
    line: int


@dataclass(frozen=True)
class AgentsDef:
    """The ``agents:`` section: where agents meet the scheduler and how long to wait for them.

    ``listen`` embeds a broker in the scheduler; ``broker`` uses an external
    one. At most one is set; with neither, agents are off. The secrets file
    holds each agent's shared secret and is read separately.
    """

    listen: tuple[str, int] | None
    broker: tuple[str, int] | None
    secrets_file: Path
    agent_grace: timedelta
    lost_after: timedelta
    source: SourceRef
    secrets: AgentSecrets = field(
        default_factory=lambda: AgentSecrets({}), repr=False, compare=False
    )


@dataclass(frozen=True)
class Dependency:
    """One entry of a job's ``depends_on`` list.

    Attributes:
        job: The upstream job's name.
        on: Which final state of the upstream satisfies it.
        run_date_offset: Business days to shift the run date by (0 or negative).
        source: Where the dependency was written.

    """

    job: str
    on: DepOn = DepOn.SUCCESS
    run_date_offset: int = 0
    source: SourceRef = field(default=SourceRef("<unknown>", 0), kw_only=True)


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how patiently a failed attempt is tried again."""

    count: int = 0
    delay: timedelta = DEFAULT_RETRY_DELAY
    backoff: float = DEFAULT_BACKOFF
    max_delay: timedelta = DEFAULT_MAX_DELAY

    def delay_for(self, n: int) -> timedelta:
        """Return the wait before retry ``n`` (1 is the first retry), capped at ``max_delay``."""
        if n < 1:
            return timedelta(0)
        try:
            grown = self.delay * self.backoff ** (n - 1)
        except OverflowError:
            return self.max_delay
        return min(grown, self.max_delay)


@dataclass(frozen=True)
class NotifyRetry:
    """How patiently a notifier tries to deliver one alert."""

    count: int = 5
    delay: timedelta = timedelta(seconds=10)
    max_delay: timedelta = timedelta(minutes=10)


@dataclass(frozen=True)
class NotifierDef:
    """One named way of telling people about an alert.

    ``options`` holds every key except ``type``, ``retries`` and
    ``timeout``, with ``${NAME}`` already expanded.
    """

    name: str
    type: str
    options: Mapping[str, object] = field(repr=False)
    retries: NotifyRetry
    timeout: timedelta
    source: SourceRef


@dataclass(frozen=True)
class Route:
    """Send alerts whose topic fits ``match`` to the ``notify`` notifiers."""

    match: str
    notify: tuple[str, ...]
    source: SourceRef


@dataclass(frozen=True)
class JobAlerts:
    """A job's alert settings: kinds forced on or off, and extra notifiers."""

    on: frozenset[AlertKind] = frozenset()
    off: frozenset[AlertKind] = frozenset()
    notify: tuple[str, ...] = ()

    def enabled(self, kind: AlertKind) -> bool:
        """Return whether this job raises alerts of ``kind`` (``on`` wins over ``off``)."""
        if kind in self.on:
            return True
        return kind not in self.off and kind not in DEFAULT_OFF


@dataclass(frozen=True)
class JobDef:
    """One job, exactly as configured, with every default filled in."""

    name: str
    command: str | tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str]
    schedules: tuple[Schedule, ...]
    manual: bool
    calendar: str | None
    timezone: ZoneInfo
    run_date_rule: RunDateRule
    depends_on: tuple[Dependency, ...]
    retries: RetryPolicy
    timeout: timedelta | None
    criticality: Criticality
    pools: Mapping[str, int]
    overlap: Overlap
    catch_up: CatchUp
    stdout: str
    stderr: str
    min_runtime: timedelta | None
    max_runtime: timedelta | None
    must_start_by: MustStartBy | None
    alerts: JobAlerts
    agent: str | None
    agent_tags: tuple[str, ...]
    tags: tuple[str, ...]
    description: str
    owner: str
    enabled: bool
    source: SourceRef
    kind: JobKind = JobKind.COMMAND
    watch: WatchSpec | None = None

    @property
    def dependency_only(self) -> bool:
        """Return whether runs come only from upstream jobs (no schedule, not manual)."""
        return not self.schedules and not self.manual and bool(self.depends_on)


@dataclass(frozen=True)
class Settings:
    """The ``scheduler:`` section: settings for the whole scheduler."""

    name: str
    timezone: ZoneInfo
    state_dir: Path
    log_dir: Path
    max_running: int
    listen: tuple[str, int]
    api_tokens_file: Path | None
    users_file: Path | None
    kill_grace: timedelta
    retention: timedelta
    memory_days: int
    watch_limit: int
    secure_cookies: bool | None = None  # None: Secure unless the listen host is loopback


@dataclass(frozen=True)
class Problem:
    """One thing wrong (or worth a warning) in a config, with its place."""

    file: str
    line: int
    message: str
    job: str | None = None
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        """Return ``file:line: job name: message`` (no job part if there is no job)."""
        where = f"{self.file}:{self.line}: "
        if self.job is not None:
            where += f"job {self.job}: "
        return where + self.message


class ConfigError(Exception):
    """Raised when a config has errors; carries every problem found."""

    def __init__(self, problems: tuple[Problem, ...]) -> None:
        """Remember the problems and list them all in the message."""
        self.problems = problems
        count = len(problems)
        lines = "\n".join(f"  {p}" for p in problems)
        super().__init__(f"config has {count} problem{'s' if count != 1 else ''}:\n{lines}")


def _frozen[K, V](mapping: Mapping[K, V]) -> Mapping[K, V]:
    """Return a read-only copy of ``mapping``."""
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class Config:
    """A whole loaded config: settings, calendars, pools, jobs and alert setup.

    The mappings are read-only. ``calendar_includes`` and
    ``calendar_sources`` keep each calendar's raw ``include`` list and
    where it was defined, so validation can look for include cycles.
    ``effective_calendars`` gives each job's calendar after inheritance (a
    dependency-only job with no calendar takes its upstreams'); a job
    missing from it uses its own ``calendar``. ``expansions`` maps each
    value a ``${NAME}`` was expanded to back to its ``NAME``, so the API
    can hide those values again.
    """

    settings: Settings
    calendars: Mapping[str, BusinessCalendar]
    pools: Mapping[str, int]
    jobs: Mapping[str, JobDef]
    warnings: tuple[Problem, ...]
    digest: str
    sources: Mapping[str, str]
    calendar_includes: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict[str, tuple[str, ...]]
    )
    calendar_sources: Mapping[str, SourceRef] = field(default_factory=dict[str, SourceRef])
    effective_calendars: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    expansions: Mapping[str, str] = field(default_factory=dict[str, str])
    notifiers: Mapping[str, NotifierDef] = field(default_factory=dict[str, NotifierDef])
    routes: tuple[Route, ...] = ()
    agents: AgentsDef | None = None
    agents_broken: bool = False
    _dependents: Mapping[str, frozenset[str]] = field(
        init=False, repr=False, compare=False, default_factory=dict[str, frozenset[str]]
    )

    def __post_init__(self) -> None:
        """Freeze the mappings and build the reverse dependency index."""
        for name in (
            "calendars",
            "pools",
            "jobs",
            "sources",
            "calendar_includes",
            "calendar_sources",
            "effective_calendars",
            "expansions",
            "notifiers",
        ):
            object.__setattr__(self, name, _frozen(getattr(self, name)))
        index: dict[str, set[str]] = {}
        for job in self.jobs.values():
            for dep in job.depends_on:
                index.setdefault(dep.job, set()).add(job.name)
        object.__setattr__(
            self, "_dependents", MappingProxyType({k: frozenset(v) for k, v in index.items()})
        )

    def calendar_for(self, job: JobDef) -> BusinessCalendar:
        """Return the job's effective business calendar (``ALL_DAYS`` when it has none).

        A dependency-only job with no calendar of its own gets the calendar
        it inherits from its upstreams.

        Raises:
            KeyError: If the job's calendar is not defined.

        """
        name = self.effective_calendars.get(job.name, job.calendar)
        if name is None:
            return ALL_DAYS
        return self.calendars[name]

    def dependents(self, job: str) -> frozenset[str]:
        """Return the names of the jobs that depend on ``job``."""
        return self._dependents.get(job, frozenset())
