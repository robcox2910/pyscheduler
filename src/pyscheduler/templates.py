"""Fill-in-the-blanks templates and ``${ENV}`` expansion.

A template is like a form letter: "Dear {name}". ``render`` fills the
blanks from a mapping, and ``placeholders_in`` lists the blanks so they
can be checked before anything runs.
"""

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from string import Formatter

PLACEHOLDERS = frozenset({"job", "run_date", "run_id", "attempt", "scheduled_at", "log_dir"})

_ENV = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FORMATTER = Formatter()


def _fields(template: str) -> list[tuple[str, str | None, str]]:
    """Return ``(name, conversion, spec)`` for each field in the template."""
    try:
        return [
            (name, conversion, spec or "")
            for _, name, spec, conversion in _FORMATTER.parse(template)
            if name is not None
        ]
    except ValueError as exc:
        msg = f"invalid template {template!r}: {exc}"
        raise ValueError(msg) from exc


def placeholders_in(template: str) -> set[str]:
    """Return the names used as ``{name}`` or ``{name:fmt}``; ``{{`` and ``}}`` are literal."""
    names: set[str] = set()
    for name, _, spec in _fields(template):
        names.add(name)
        if "{" in spec:
            names |= placeholders_in(spec)
    return names


def _format_value(value: object, spec: str) -> str:
    """Format one value, using strftime or isoformat for dates."""
    if isinstance(value, date):
        if spec:
            return value.strftime(spec)
        if isinstance(value, datetime):
            if value.utcoffset() == timedelta(0) and value.tzinfo is not None:
                return value.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
            return value.isoformat()
        return value.isoformat()
    return format(value, spec)


def render(template: str, values: Mapping[str, object]) -> str:
    """Fill a template from ``values``.

    A ``date`` or ``datetime`` with a format spec uses ``strftime``; without
    one it uses ``isoformat()`` (aware UTC datetimes end in ``Z``).

    Raises:
        KeyError: If a name is missing from ``values``.
        ValueError: If the template is malformed.

    """
    parts: list[str] = []
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        msg = f"invalid template {template!r}: {exc}"
        raise ValueError(msg) from exc
    for literal, name, spec, conversion in parsed:
        parts.append(literal)
        if name is None:
            continue
        if spec and "{" in spec:
            msg = f"invalid template {template!r}: nested fields are not supported"
            raise ValueError(msg)
        value = values[name]
        if conversion:
            value = _FORMATTER.convert_field(value, conversion)
        parts.append(_format_value(value, spec or ""))
    return "".join(parts)


def expand_env(text: str, env: Mapping[str, str]) -> str:
    """Expand ``${NAME}`` from ``env``; ``$$`` becomes ``$``.

    Raises:
        KeyError: With the variable name, if it is not in ``env``.

    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return "$" if name is None else env[name]

    return _ENV.sub(replace, text)
