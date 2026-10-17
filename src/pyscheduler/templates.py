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


def _parse(template: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """Parse a template into ``(literal, name, spec, conversion)`` tuples.

    Raises:
        ValueError: If the template is malformed or has nested fields in a spec.

    """
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        msg = f"invalid template {template!r}: {exc}"
        raise ValueError(msg) from exc
    for _, name, spec, _ in parsed:
        if name is not None and spec and "{" in spec:
            msg = f"invalid template {template!r}: nested fields are not supported"
            raise ValueError(msg)
    return parsed


def placeholders_in(template: str) -> set[str]:
    """Return the names used as ``{name}`` or ``{name:fmt}``; ``{{`` and ``}}`` are literal.

    Raises:
        ValueError: If the template is malformed.

    """
    return {name for _, name, _, _ in _parse(template) if name is not None}


def _format_value(value: object, spec: str) -> str:
    """Format one value, using strftime or isoformat for dates."""
    if isinstance(value, date):
        if spec:
            return value.strftime(spec)
        if isinstance(value, datetime) and value.utcoffset() == timedelta(0):
            return value.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
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
    for literal, name, spec, conversion in _parse(template):
        parts.append(literal)
        if name is None:
            continue
        value = values[name]
        if conversion:
            value = _FORMATTER.convert_field(value, conversion)
        try:
            parts.append(_format_value(value, spec or ""))
        except TypeError as exc:
            msg = f"invalid template {template!r}: {exc}"
            raise ValueError(msg) from exc
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
