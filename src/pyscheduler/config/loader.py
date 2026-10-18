"""Load config files into a ``Config``, collecting every problem on the way.

Loading is like unpacking a delivery against the order form. The main file
is opened first, then every file it ``include:``s. Each box is checked
and anything wrong goes on one list, so you hear about all the problems at
once instead of fixing them one reload at a time.
"""

import hashlib
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from pyscheduler.calendars import BusinessCalendar, merge_calendars
from pyscheduler.config.alert_fields import read_alert_sections
from pyscheduler.config.calendar_fields import RawCalendar, reachable, read_calendar
from pyscheduler.config.calendar_inheritance import resolve_calendars
from pyscheduler.config.fields import (
    Reader,
    RecordingEnv,
    check_keys,
    read_int,
    read_map,
    read_seq,
    read_str,
)
from pyscheduler.config.job_fields import JobFields, build_job, merge_fields, read_job_fields
from pyscheduler.config.model import Config, ConfigError, JobDef, Problem, Settings, SourceRef
from pyscheduler.config.redaction import redactor
from pyscheduler.config.settings_fields import read_settings
from pyscheduler.config.validate import validate_config
from pyscheduler.config.watch_fields import check_kind_keys
from pyscheduler.values import check_name
from pyscheduler.yamlish import Map, Node, YamlError, parse

MAIN_KEYS = (
    "scheduler", "include", "calendars", "pools", "defaults", "jobs", "notifiers", "routes",
)  # fmt: skip
INCLUDED_KEYS = ("include", "calendars", "pools", "jobs")
RESERVED_JOB = "_scheduler"
GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class _Doc:
    """One parsed config file."""

    path: Path
    root: Map


class _Loader:
    """Read the main file and its includes, collecting problems."""

    def __init__(self, main: Path, env: Mapping[str, str]) -> None:
        """Prepare to load ``main``."""
        self.main = main
        self.env = RecordingEnv(env)
        self.r = Reader(os.path.normpath(main), self.env)
        self.docs: list[_Doc] = []
        self.sources: dict[str, str] = {}
        self.seen: set[Path] = set()

    # --- files ---------------------------------------------------------

    def read_file(self, path: Path, r: Reader, line: int) -> None:
        """Parse ``path`` and, depth first, the files it includes."""
        name = os.path.normpath(path)
        key = path.resolve()
        if key in self.seen:
            return
        self.seen.add(key)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            r.add(line, f"cannot read config file {name}: {exc.strerror or exc}")
            return
        except UnicodeDecodeError as exc:
            r.add(line, f"cannot read config file {name}: not valid UTF-8 ({exc.reason})")
            return
        self.sources[name] = text
        here = r.for_file(name)
        try:
            root = parse(text, name)
        except YamlError as exc:
            here.add(exc.line, exc.message)
            return
        except RecursionError:
            here.add(1, "nesting too deep")
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
                r = self.r.for_file(os.path.normpath(doc.path))
                mapping = read_map(r, doc.root.items[key], key)
                if mapping is not None:
                    out.append((r, mapping))
        return out

    def main_section(self, key: str) -> tuple[Reader, Map] | None:
        """Return the reader and mapping for ``key`` in the main file only."""
        if not self.docs or self.docs[0].path != self.main or key not in self.docs[0].root.items:
            return None
        mapping = read_map(self.r, self.docs[0].root.items[key], key)
        return None if mapping is None else (self.r, mapping)

    # --- sections ------------------------------------------------------

    def settings(self) -> Settings:
        """Read the ``scheduler:`` section."""
        found = self.main_section("scheduler")
        if found is None:
            return read_settings(self.r, Map({}, {}, 1), self.main)
        return read_settings(*found, self.main)

    def calendars(self) -> tuple[dict[str, BusinessCalendar], dict[str, RawCalendar]]:
        """Read every calendar, then merge each one's included holidays."""
        raw: dict[str, RawCalendar] = {}
        seen: dict[str, SourceRef] = {}
        for r, section in self.readers("calendars"):
            for name, node in section.items.items():
                line = section.key_lines[name]
                if _named(r, name, line, seen.get(name), "calendar"):
                    seen[name] = SourceRef(r.file, line)
                    cal = read_calendar(r, name, node, line)
                    if cal is not None:
                        raw[name] = cal
        merged: dict[str, BusinessCalendar] = {}
        for name, cal in raw.items():
            for other, line in cal.includes:
                if other not in raw:
                    self.r.for_file(cal.source.file).add(line, f"unknown calendar {other!r}")
            included = [raw[other].calendar for other in reachable(name, raw)]
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
        seen: dict[str, SourceRef] = {}
        for r, section in self.readers("jobs"):
            for name, node in section.items.items():
                line = section.key_lines[name]
                jr = r.for_job(name)
                if not _named(jr, name, line, seen.get(name), "job"):
                    continue
                seen[name] = SourceRef(r.file, line)
                mapping = read_map(jr, node, f"job {name}")
                if mapping is None:
                    continue
                own, suggested = read_job_fields(jr, mapping, f"job {name}")
                fields = merge_fields(defaults, own)
                has_command = (
                    fields.command is not None or "command" in mapping.items.keys() | suggested
                )
                check_kind_keys(jr, line, own.type, mapping, suggested, has_command=has_command)
                jobs[name] = build_job(jr, name, fields, SourceRef(r.file, line), zone)
        return jobs


def _named(r: Reader, name: str, line: int, first: SourceRef | None, kind: str) -> bool:
    """Check a name's spelling and that it is new; return whether it is new.

    ``first`` is where a thing of the same name was already defined, if anywhere.
    """
    try:
        check_name(name)
    except ValueError as exc:
        r.add(line, str(exc))
    if kind == "job" and name == RESERVED_JOB:
        r.add(line, f"job name {name!r} is reserved")
    if first is None:
        return True
    r.add(line, f"duplicate {kind} {name!r}: first defined at {first.file}:{first.line}")
    return False


def _matches(base: Path, pattern: str) -> list[Path]:
    """Return the files an include pattern names, relative to ``base``, sorted."""
    path = Path(pattern)
    if not path.is_absolute():
        path = base / path
    if not GLOB_CHARS & set(pattern):
        return [path] if path.is_file() else []
    anchor = Path(path.anchor)
    return sorted(p for p in anchor.glob(str(path.relative_to(anchor))) if p.is_file())


def _digest(sources: Mapping[str, str]) -> str:
    """Return a sha256 over each source's path and text, in load order.

    Each path and text ends with a NUL byte, so moving text from one file
    to the next (or renaming a file) changes the digest.
    """
    sha = hashlib.sha256()
    for name, text in sources.items():
        sha.update(name.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return sha.hexdigest()


def build_config(
    path: Path, *, env: Mapping[str, str] | None = None
) -> tuple[Config, list[Problem]]:
    """Load ``path`` and its includes, checking each field.

    Only field-level problems are found here; ``load_config`` adds the
    checks that need the whole config. No problem message shows a value a
    ``${NAME}`` expanded to: each is put back as ``${NAME}``. When there are errors, the returned
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
    main = loader.docs[0].root.items if loader.docs and loader.docs[0].path == loader.main else {}
    notifiers, routes = read_alert_sections(loader.r, main)
    order = {name: i for i, name in enumerate(loader.sources)}
    expansions = {value: name for name, value in loader.env.used.items()}
    problems = sorted(
        _redacted(loader.r.problems, expansions), key=lambda p: (order.get(p.file, -1), p.line)
    )
    digest = _digest(loader.sources)
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
        effective_calendars=resolve_calendars(jobs).effective,
        expansions=expansions,
        notifiers=notifiers,
        routes=routes,
    )
    return config, problems


def _redacted(problems: Iterable[Problem], expansions: Mapping[str, str]) -> list[Problem]:
    """Return ``problems`` with every expanded value put back as ``${NAME}`` (ruling R29)."""
    hide = redactor(expansions)
    return [replace(p, message=hide(p.message)) for p in problems]


def load_config(
    path: Path, *, env: Mapping[str, str] | None = None, today: date | None = None
) -> Config:
    """Load ``path`` into a ``Config``, checking each field and then the whole.

    The semantic checks of ``validate_config`` run even when fields had
    errors, so every problem is reported at once. Jobs with field errors
    are left out of those checks, so one mistake is not reported twice.
    That includes a job whose name is used twice: its semantic checks are
    skipped until the duplicate is fixed.

    Args:
        path: The main config file.
        env: Variables for ``${NAME}`` expansion; ``None`` means ``os.environ``.
        today: Where the validation horizons start; default today in UTC.

    Raises:
        ConfigError: If there is any error (warnings do not stop the load).

    """
    config, problems = build_config(path, env=env)
    broken = frozenset(p.job for p in problems if p.job is not None and p.severity == "error")
    order = {name: i for i, name in enumerate(config.sources)}
    checked = validate_config(config, today=today, broken_jobs=broken)
    problems = sorted(
        [*problems, *_redacted(checked, config.expansions)],
        key=lambda p: (order.get(p.file, -1), p.line),
    )
    errors = tuple(p for p in problems if p.severity == "error")
    if errors:
        raise ConfigError(errors)
    return replace(config, warnings=tuple(p for p in problems if p.severity == "warning"))
