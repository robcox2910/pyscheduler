"""Typed field readers: turn YAML nodes into checked Python values.

Each reader is a careful clerk with a form. It looks at one box, writes
down what is wrong if the box is filled in badly, and moves on to the next
box instead of giving up. That way one pass over a config finds **every**
problem, not just the first.

A reader returns ``None`` when the value is missing or bad (after noting
the problem), so callers fall back to the default.
"""

import difflib
import math
import re
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pyscheduler.config.model import MustStartBy, Problem
from pyscheduler.templates import expand_env
from pyscheduler.values import parse_date, parse_duration, parse_time
from pyscheduler.yamlish import Map, Node, Scalar, Seq

_INT = re.compile(r"[-+]?\d+", re.ASCII)
_FLOAT = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)", re.ASCII)
MAX_INT_CHARS = 20
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class RecordingEnv(Mapping[str, str]):
    """A read-only view of the environment that remembers every variable looked up.

    The loader hands this to its readers, so afterwards it knows each
    ``${NAME}`` it expanded and the value it put in (the API hides those
    values again before showing a config).
    """

    def __init__(self, env: Mapping[str, str]) -> None:
        """Wrap ``env``; nothing has been looked up yet."""
        self._env = env
        self.used: dict[str, str] = {}

    def __getitem__(self, name: str) -> str:
        """Return the variable ``name`` and remember that it was used."""
        value = self._env[name]
        self.used[name] = value
        return value

    def __contains__(self, name: object) -> bool:
        """Return whether ``name`` is set, without counting it as used."""
        return name in self._env

    def __iter__(self) -> Iterator[str]:
        """Iterate over the variable names."""
        return iter(self._env)

    def __len__(self) -> int:
        """Return how many variables there are."""
        return len(self._env)


@dataclass
class Reader:
    """Where values come from, and where problems go.

    Attributes:
        file: The file being read, used in problems.
        env: Variables for ``${NAME}`` expansion.
        problems: The shared list every problem is appended to.
        job: The job being read, if any.

    """

    file: str
    env: Mapping[str, str]
    problems: list[Problem] = field(default_factory=list[Problem])
    job: str | None = None

    def add(self, line: int, message: str, severity: Literal["error", "warning"] = "error") -> None:
        """Note a problem at ``line`` of this reader's file."""
        self.problems.append(Problem(self.file, line, message, self.job, severity))

    def for_job(self, job: str | None) -> Reader:
        """Return a reader for the same file and problem list, tagged with ``job``."""
        return Reader(self.file, self.env, self.problems, job)

    def for_file(self, file: str) -> Reader:
        """Return a reader for another file that shares the problem list."""
        return Reader(file, self.env, self.problems, self.job)


def _describe(node: Node) -> str:
    """Return a short name for a node's kind, for messages."""
    if isinstance(node, Map):
        return "a mapping"
    if isinstance(node, Seq):
        return "a list"
    return "text"


def check_keys(r: Reader, node: Map, allowed: Collection[str], where: str) -> set[str]:
    """Note every key of ``node`` not in ``allowed``, suggesting a close match.

    Returns:
        The allowed names that were suggested, so callers can skip follow-on
        "missing key" complaints about a key that was merely misspelt.

    """
    suggested: set[str] = set()
    for key in node.items:
        if key in allowed:
            continue
        message = f"unknown key {key!r} in {where}"
        close = difflib.get_close_matches(key, list(allowed), n=1)
        if close:
            message += f"; did you mean {close[0]!r}?"
            suggested.add(close[0])
        r.add(node.key_lines[key], message)
    return suggested


def read_map(r: Reader, node: Node, what: str) -> Map | None:
    """Return ``node`` if it is a mapping, else note a problem."""
    if isinstance(node, Map):
        return node
    r.add(node.line, f"{what} must be a mapping, got {_describe(node)}")
    return None


def read_seq(r: Reader, node: Node, what: str) -> list[Node] | None:
    """Return the items of ``node`` if it is a list, else note a problem."""
    if isinstance(node, Seq):
        return node.items
    r.add(node.line, f"{what} must be a list, got {_describe(node)}")
    return None


def read_str(r: Reader, node: Node, what: str, *, template: bool = False) -> str | None:
    """Return a scalar's text with ``${NAME}`` expanded, or ``None`` on a problem.

    ``template`` marks a field that is later rendered as a template: the
    braces of expanded values are doubled so they render back literally.
    """
    if not isinstance(node, Scalar):
        r.add(node.line, f"{what} must be text, got {_describe(node)}")
        return None
    try:
        return expand_env(node.value, r.env, template=template)
    except KeyError as exc:
        r.add(node.line, f"{what}: environment variable {exc.args[0]!r} is not set")
        return None


def _convert[T](r: Reader, node: Node, what: str, convert: Callable[[str], T]) -> T | None:
    """Read text and convert it, noting a ``ValueError`` as a problem."""
    text = read_str(r, node, what)
    if text is None:
        return None
    try:
        return convert(text)
    except ValueError as exc:
        r.add(node.line, f"{what}: {exc}")
        return None


def read_int(r: Reader, node: Node, what: str, *, minimum: int | None = None) -> int | None:
    """Return a whole number, optionally at least ``minimum``."""
    text = read_str(r, node, what)
    if text is None:
        return None
    if _INT.fullmatch(text) is None:
        r.add(node.line, f"{what} must be an integer, got {text!r}")
        return None
    if len(text) > MAX_INT_CHARS:
        r.add(node.line, f"{what} is too large ({len(text)} characters)")
        return None
    value = int(text)
    if minimum is not None and value < minimum:
        r.add(node.line, f"{what} must be at least {minimum}, got {value}")
        return None
    return value


def read_float(r: Reader, node: Node, what: str) -> float | None:
    """Return a finite decimal number such as ``2`` or ``1.5``."""
    text = read_str(r, node, what)
    if text is None:
        return None
    if _FLOAT.fullmatch(text) is None or not math.isfinite(float(text)):
        r.add(node.line, f"{what} must be a number, got {text!r}")
        return None
    return float(text)


def read_bool(r: Reader, node: Node, what: str) -> bool | None:
    """Return ``True`` or ``False`` from exactly ``true`` or ``false``."""
    text = read_str(r, node, what)
    if text is None:
        return None
    if text not in {"true", "false"}:
        r.add(node.line, f"{what} must be true or false, got {text!r}")
        return None
    return text == "true"


def read_duration(r: Reader, node: Node, what: str) -> timedelta | None:
    """Return a duration such as ``90s`` or ``1h30m``."""
    return _convert(r, node, what, parse_duration)


def read_time(r: Reader, node: Node, what: str) -> time | None:
    """Return a clock time ``HH:MM``."""
    return _convert(r, node, what, parse_time)


def read_date(r: Reader, node: Node, what: str) -> date | None:
    """Return an ISO date ``YYYY-MM-DD``."""
    return _convert(r, node, what, parse_date)


def read_must_start_by(r: Reader, node: Node, what: str) -> MustStartBy | None:
    """Return a clock time (``HH:MM``) or a duration after the scheduled time."""

    def convert(text: str) -> MustStartBy:
        """Parse a time if it has a colon, else a duration."""
        return parse_time(text) if ":" in text else parse_duration(text)

    return _convert(r, node, what, convert)


def read_zone(r: Reader, node: Node, what: str) -> ZoneInfo | None:
    """Return the time zone named by a scalar, such as ``Europe/London``."""

    def convert(text: str) -> ZoneInfo:
        """Look up the zone, turning lookup failures into ``ValueError``."""
        try:
            return ZoneInfo(text)
        except ZoneInfoNotFoundError, ValueError:
            msg = f"unknown time zone {text!r}"
            raise ValueError(msg) from None

    return _convert(r, node, what, convert)


def read_enum[E: StrEnum](r: Reader, node: Node, what: str, enum: type[E]) -> E | None:
    """Return the member of a string enum whose value is the text."""

    def convert(text: str) -> E:
        """Look up the member, listing the choices on failure."""
        try:
            return enum(text)
        except ValueError:
            choices = ", ".join(m.value for m in enum)
            msg = f"unknown value {text!r}: use one of {choices}"
            raise ValueError(msg) from None

    return _convert(r, node, what, convert)


def read_list[T](
    r: Reader, node: Node, what: str, item: Callable[[Reader, Node, str], T | None]
) -> list[T] | None:
    """Return a list whose items are each read by ``item``; bad items are dropped."""
    items = read_seq(r, node, what)
    if items is None:
        return None
    values = (item(r, child, what) for child in items)
    return [v for v in values if v is not None]


def read_str_list(
    r: Reader, node: Node, what: str, *, template: bool = False
) -> tuple[str, ...] | None:
    """Return a list of text values as a tuple (``template``: see ``read_str``)."""
    values = read_list(r, node, what, lambda r, n, w: read_str(r, n, w, template=template))
    return None if values is None else tuple(values)


def read_template(r: Reader, node: Node, what: str) -> str | None:
    """Return a template field's text (expanded values have their braces doubled)."""
    return read_str(r, node, what, template=True)


def read_weekday(r: Reader, node: Node, what: str) -> int | None:
    """Return a weekday number (0 is Monday) from a name ``mon`` .. ``sun``."""
    text = read_str(r, node, what)
    if text is None:
        return None
    if text not in WEEKDAYS:
        r.add(node.line, f"{what}: unknown weekday {text!r}: use mon, tue, ... sun")
        return None
    return WEEKDAYS.index(text)


def read_str_map(
    r: Reader, node: Node, what: str, *, template: bool = False
) -> dict[str, str] | None:
    """Return a mapping of names to text values (``template``: see ``read_str``)."""
    mapping = read_map(r, node, what)
    if mapping is None:
        return None
    out: dict[str, str] = {}
    for key, child in mapping.items.items():
        value = read_str(r, child, f"{what}.{key}", template=template)
        if value is not None:
            out[key] = value
    return out


def read_plain(r: Reader, node: Node, what: str) -> object:
    """Return a node as plain Python: text, lists and dicts (``${NAME}`` expanded)."""
    if isinstance(node, Map):
        return {key: read_plain(r, child, f"{what}.{key}") for key, child in node.items.items()}
    if isinstance(node, Seq):
        return [read_plain(r, child, what) for child in node.items]
    return read_str(r, node, what)
