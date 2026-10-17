"""Load config files into a ``Config``, collecting every problem on the way.

Loading is like unpacking a delivery against the order form. The main file
is opened first, then every file it ``include:``s. Each box is checked
and anything wrong goes on one list, so you hear about all the problems at
once instead of fixing them one reload at a time.
"""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pyscheduler.calendars import BusinessCalendar, merge_calendars
from pyscheduler.config.fields import (
    Reader,
    check_keys,
    read_date,
    read_duration,
    read_int,
    read_list,
    read_map,
    read_seq,
    read_str,
    read_str_list,
    read_weekday,
    read_zone,
)
from pyscheduler.config.job_fields import JobFields, build_job, merge_fields, read_job_fields
from pyscheduler.config.model import Config, ConfigError, JobDef, Problem, Settings, SourceRef
from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, YamlError, parse

MAIN_KEYS = ("scheduler", "include", "calendars", "pools", "defaults", "jobs")
INCLUDED_KEYS = ("include", "calendars", "pools", "jobs")
SCHEDULER_KEYS = (
    "name", "timezone", "state_dir", "log_dir", "max_running", "listen",
    "api_tokens_file", "kill_grace", "retention", "memory_days",
)  # fmt: skip
CALENDAR_KEYS = ("weekdays", "holidays", "extra_days", "include")
GLOB_CHARS = frozenset("*?[")
MAX_PORT = 65535
DEFAULT_NAME = "pysched"
DEFAULT_MAX_RUNNING = 32
DEFAULT_LISTEN = ("127.0.0.1", 8470)
DEFAULT_KILL_GRACE = timedelta(seconds=10)
DEFAULT_RETENTION = timedelta(days=90)
DEFAULT_MEMORY_DAYS = 7
DEFAULT_WEEKDAYS = frozenset(range(5))
UTC_ZONE = ZoneInfo("UTC")


@dataclass(frozen=True)
class _Doc:
    """One parsed config file."""

    path: Path
    root: Map


@dataclass(frozen=True)
class _RawCalendar:
    """A calendar as written, before its includes are merged in."""

    calendar: BusinessCalendar
    includes: tuple[tuple[str, int], ...]
    source: SourceRef


class _Loader:
    """Read the main file and its includes, collecting problems."""

    def __init__(self, main: Path, env: Mapping[str, str]) -> None:
        """Prepare to load ``main``."""
        self.main = main
        self.r = Reader(str(main), env)
        self.docs: list[_Doc] = []
        self.sources: dict[str, str] = {}
        self.seen: set[Path] = set()

    # --- files ---------------------------------------------------------

    def read_file(self, path: Path, r: Reader, line: int) -> None:
        """Parse ``path`` and, depth first, the files it includes."""
        name = str(path)
        key = path.resolve()
        if key in self.seen:
            return
        self.seen.add(key)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            r.add(line, f"cannot read config file {name}: {exc.strerror or exc}")
            return
        self.sources[name] = text
        here = r.for_file(name)
        try:
            root = parse(text, name)
        except YamlError as exc:
            here.add(exc.line, exc.message)
            return
        if not isinstance(root, Map):
            here.add(root.line, "the top level of a config file must be a mapping")
            return
        is_main = not self.docs
        check_keys(here, root, MAIN_KEYS, "the config file")
        if not is_main:
            for key in root.items.keys() - set(INCLUDED_KEYS):
                if key in MAIN_KEYS:
                    here.add(root.key_lines[key], f"{key} is only allowed in the main file")
        self.docs.append(_Doc(path, root))
        if "include" in root.items:
            self.read_includes(path.parent, here, root.items["include"])

    def read_includes(self, base: Path, r: Reader, node: Node) -> None:
        """Read every file matched by the ``include:`` patterns."""
        for item in read_seq(r, node, "include") or []:
            pattern = read_str(r, item, "include")
            if pattern is None:
                continue
            matches = _matches(base, pattern)
            if not matches:
                r.add(item.line, f"include {pattern!r} matched no files")
            for path in matches:
                self.read_file(path, r, item.line)

    def readers(self, key: str) -> list[tuple[Reader, Map]]:
        """Return a reader and the mapping for ``key`` in each file that has it."""
        out: list[tuple[Reader, Map]] = []
        for doc in self.docs:
            if key in doc.root.items:
                r = self.r.for_file(str(doc.path))
                mapping = read_map(r, doc.root.items[key], key)
                if mapping is not None:
                    out.append((r, mapping))
        return out

    def main_section(self, key: str) -> tuple[Reader, Map] | None:
        """Return the reader and mapping for ``key`` in the main file only."""
        found = self.readers(key)
        if found and found[0][0].file == str(self.main):
            return found[0]
        return None

    # --- sections ------------------------------------------------------

    def settings(self) -> Settings:
        """Read the ``scheduler:`` section."""
        found = self.main_section("scheduler")
        if found is None:
            return _settings(self.r, Map({}, {}, 1), self.main)
        return _settings(*found, self.main)

    def calendars(self) -> tuple[dict[str, BusinessCalendar], dict[str, _RawCalendar]]:
        """Read every calendar, then merge each one's included holidays."""
        raw: dict[str, _RawCalendar] = {}
        for r, section in self.readers("calendars"):
            for name, node in section.items.items():
                line = section.key_lines[name]
                if _named(r, name, line, _source(raw.get(name)), "calendar"):
                    cal = _calendar(r, name, node, line)
                    if cal is not None:
                        raw[name] = cal
        merged: dict[str, BusinessCalendar] = {}
        for name, cal in raw.items():
            included: list[BusinessCalendar] = []
            for other, line in cal.includes:
                if other in raw:
                    included.append(raw[other].calendar)
                else:
                    self.r.for_file(cal.source.file).add(line, f"unknown calendar {other!r}")
            merged[name] = merge_calendars(name, cal.calendar, included)
        return merged, raw

    def pools(self) -> dict[str, int]:
        """Read every pool's size."""
        pools: dict[str, int] = {}
        seen: dict[str, SourceRef] = {}
        for r, section in self.readers("pools"):
            for name, node in section.items.items():
                line = section.key_lines[name]
                if _named(r, name, line, seen.get(name), "pool"):
                    seen[name] = SourceRef(r.file, line)
                    size = read_int(r, node, f"pool {name}", minimum=1)
                    if size is not None:
                        pools[name] = size
        return pools

    def jobs(self, zone: ZoneInfo) -> dict[str, JobDef]:
        """Read every job, laying it over the ``defaults:`` section."""
        defaults = JobFields()
        found = self.main_section("defaults")
        if found is not None:
            defaults, _ = read_job_fields(found[0], found[1], "defaults")
        jobs: dict[str, JobDef] = {}
        for r, section in self.readers("jobs"):
            for name, node in section.items.items():
                line = section.key_lines[name]
                jr = r.for_job(name)
                if not _named(jr, name, line, _source(jobs.get(name)), "job"):
                    continue
                mapping = read_map(jr, node, f"job {name}")
                if mapping is None:
                    continue
                own, suggested = read_job_fields(jr, mapping, f"job {name}")
                fields = merge_fields(defaults, own)
                if fields.command is None and "command" not in mapping.items.keys() | suggested:
                    jr.add(line, "missing required key 'command'")
                jobs[name] = build_job(name, fields, SourceRef(r.file, line), zone)
        return jobs


def _named(r: Reader, name: str, line: int, first: SourceRef | None, kind: str) -> bool:
    """Check a name's spelling and that it is new; return whether it is new.

    ``first`` is where a thing of the same name was already defined, if anywhere.
    """
    try:
        check_name(name)
    except ValueError as exc:
        r.add(line, str(exc))
    if first is None:
        return True
    r.add(line, f"duplicate {kind} {name!r}: first defined at {first.file}:{first.line}")
    return False


def _source(thing: _RawCalendar | JobDef | None) -> SourceRef | None:
    """Return where ``thing`` was defined, if it exists."""
    return None if thing is None else thing.source


def _matches(base: Path, pattern: str) -> list[Path]:
    """Return the files an include pattern names, relative to ``base``, sorted."""
    path = Path(pattern)
    if not path.is_absolute():
        path = base / path
    if not GLOB_CHARS & set(pattern):
        return [path] if path.is_file() else []
    anchor = Path(path.anchor)
    return sorted(p for p in anchor.glob(str(path.relative_to(anchor))) if p.is_file())


def _listen(r: Reader, node: Node) -> tuple[str, int] | None:
    """Read ``listen: host:port``."""
    text = read_str(r, node, "listen")
    if text is None:
        return None
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or int(port) > MAX_PORT:
        r.add(node.line, f"listen must be host:port with a port 0-{MAX_PORT}, got {text!r}")
        return None
    return host, int(port)


def _settings(r: Reader, node: Map, main: Path) -> Settings:
    """Read the scheduler settings, resolving paths next to the main file."""
    check_keys(r, node, SCHEDULER_KEYS, "scheduler")
    items = node.items
    base = main.absolute().parent

    def path(key: str) -> Path | None:
        """Read a path setting, relative to the main file's folder."""
        text = read_str(r, items[key], key) if key in items else None
        return None if text is None else base / text

    def count(key: str, default: int) -> int:
        """Read a positive whole-number setting."""
        value = read_int(r, items[key], key, minimum=1) if key in items else None
        return default if value is None else value

    name = read_str(r, items["name"], "name") if "name" in items else None
    zone = read_zone(r, items["timezone"], "timezone") if "timezone" in items else None
    listen = _listen(r, items["listen"]) if "listen" in items else None
    return Settings(
        name=name or DEFAULT_NAME,
        timezone=zone or UTC_ZONE,
        state_dir=path("state_dir") or base / "state",
        log_dir=path("log_dir") or base / "logs",
        max_running=count("max_running", DEFAULT_MAX_RUNNING),
        listen=listen or DEFAULT_LISTEN,
        api_tokens_file=path("api_tokens_file"),
        kill_grace=_duration(r, items, "kill_grace") or DEFAULT_KILL_GRACE,
        retention=_duration(r, items, "retention") or DEFAULT_RETENTION,
        memory_days=count("memory_days", DEFAULT_MEMORY_DAYS),
    )


def _duration(r: Reader, items: Mapping[str, Node], key: str) -> timedelta | None:
    """Read an optional duration setting."""
    return read_duration(r, items[key], key) if key in items else None


def _calendar(r: Reader, name: str, node: Node, line: int) -> _RawCalendar | None:
    """Read one calendar definition."""
    mapping = read_map(r, node, f"calendar {name}")
    if mapping is None:
        return None
    check_keys(r, mapping, CALENDAR_KEYS, f"calendar {name}")
    items = mapping.items
    weekdays = DEFAULT_WEEKDAYS
    if "weekdays" in items:
        weekdays = frozenset(read_list(r, items["weekdays"], "weekdays", read_weekday) or ())

    def dates(key: str) -> frozenset[date]:
        """Read a list of ISO dates."""
        if key not in items:
            return frozenset()
        return frozenset(read_list(r, items[key], key, read_date) or ())

    includes: list[tuple[str, int]] = []
    if "include" in items:
        nodes = read_seq(r, items["include"], "include") or []
        names = read_str_list(r, items["include"], "include") or ()
        includes = [(n, item.line) for n, item in zip(names, nodes, strict=False)]
    cal = BusinessCalendar(name, weekdays, dates("holidays"), dates("extra_days"))
    return _RawCalendar(cal, tuple(includes), SourceRef(r.file, line))


def build_config(
    path: Path, *, env: Mapping[str, str] | None = None
) -> tuple[Config, list[Problem]]:
    """Load ``path`` and its includes, checking each field.

    Only field-level problems are found here; ``load_config`` adds the
    checks that need the whole config. When there are errors, the returned
    ``Config`` is partial and must not be used.

    Args:
        path: The main config file.
        env: Variables for ``${NAME}`` expansion; ``None`` means ``os.environ``.

    Returns:
        The config and every problem found, sorted by file and line.

    """
    if env is None:
        env = dict(os.environ)
    loader = _Loader(Path(path), env)
    loader.read_file(loader.main, loader.r, 1)
    settings = loader.settings()
    calendars, raw = loader.calendars()
    pools = loader.pools()
    jobs = loader.jobs(settings.timezone)
    order = {name: i for i, name in enumerate(loader.sources)}
    problems = sorted(loader.r.problems, key=lambda p: (order.get(p.file, -1), p.line))
    digest = hashlib.sha256("".join(loader.sources.values()).encode("utf-8")).hexdigest()
    config = Config(
        settings=settings,
        calendars=calendars,
        pools=pools,
        jobs=jobs,
        warnings=tuple(p for p in problems if p.severity == "warning"),
        digest=digest,
        sources=loader.sources,
        calendar_includes={n: tuple(i for i, _ in c.includes) for n, c in raw.items()},
        calendar_sources={n: c.source for n, c in raw.items()},
    )
    return config, problems


def load_config(path: Path, *, env: Mapping[str, str] | None = None) -> Config:
    """Load ``path`` into a ``Config``.

    Raises:
        ConfigError: If there is any error (warnings do not stop the load).

    """
    config, problems = build_config(path, env=env)
    errors = tuple(p for p in problems if p.severity == "error")
    if errors:
        raise ConfigError(errors)
    return config
