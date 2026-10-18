"""Hide the values ``${NAME}`` expanded to, by putting ``${NAME}`` back.

A config may take a password from the environment. Wherever the config
shows text to people (a problem message, a job's definition in the API),
that password must not appear, so each expanded value is swapped back
for the ``${NAME}`` it came from:

```python
from pyscheduler.config.redaction import redact_text

print(redact_text("login --pw hunter22", {"hunter22": "DB_PASSWORD"}))
# login --pw ${DB_PASSWORD}
```

Longer values go first, so a value that contains another is hidden whole.
Values shorter than ``MIN_REDACTED`` characters are left alone: hiding
every ``1`` or ``yes`` would mangle ordinary text. A ``${NAME}`` already
in the text is never touched, so redacting twice changes nothing more.
"""

import re
from collections.abc import Callable, Mapping

__all__ = ["MIN_REDACTED", "escape_braces", "redact_text", "redactor"]

MIN_REDACTED = 4
"""The shortest expanded value that is hidden again."""

_PLACEHOLDER = r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"


def escape_braces(text: str) -> str:
    """Double every brace, so a template renders ``text`` back literally."""
    return text.replace("{", "{{").replace("}", "}}")


def redactor(expansions: Mapping[str, str]) -> Callable[[str], str]:
    """Return a function that hides every value of ``expansions`` (value -> name) in a text.

    The brace-doubled form of a value (as stored in template fields) is
    hidden too.
    """
    names: dict[str, str] = {}
    for value, name in expansions.items():
        if len(value) >= MIN_REDACTED:
            names[value] = name
            names.setdefault(escape_braces(value), name)
    if not names:
        return lambda text: text
    values = sorted(names, key=len, reverse=True)
    pattern = re.compile("|".join([_PLACEHOLDER, *(re.escape(v) for v in values)]))

    def swap(match: re.Match[str]) -> str:
        """Put ``${NAME}`` in place of a value; keep an existing ``${NAME}``."""
        found = match.group(0)
        name = names.get(found)
        return found if name is None else f"${{{name}}}"

    return lambda text: pattern.sub(swap, text)


def redact_text(text: str, expansions: Mapping[str, str]) -> str:
    """Return ``text`` with every expanded value put back as ``${NAME}``."""
    return redactor(expansions)(text)
