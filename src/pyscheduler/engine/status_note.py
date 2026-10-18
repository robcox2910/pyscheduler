"""How an attempt ended, and reading the minder's status note that says so.

The minder leaves a small JSON note when the job ends (its exit code or
signal, when it started and ended, or why it could not start).
``read_status`` turns that note into an ``AttemptResult``; a note that
is there but cannot be understood still gives a result, never an error.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

__all__ = ["CORRUPT_PREFIX", "AttemptResult", "read_status"]

CORRUPT_PREFIX = "corrupt status file: "


@dataclass(frozen=True)
class AttemptResult:
    """How one attempt ended.

    Attributes:
        exit_code: The job's exit code, or None if a signal ended it or it never started.
        signal: The signal number that ended it, or None.
        started_at: When the job started, or None if unknown.
        ended_at: When it ended (or when we found out).
        start_error: Why it could not start or be read, or None.

    """

    exit_code: int | None
    signal: int | None
    started_at: datetime | None
    ended_at: datetime
    start_error: str | None

    @property
    def succeeded(self) -> bool:
        """Return whether the job exited 0 with no signal and no start error."""
        return self.exit_code == 0 and self.signal is None and self.start_error is None


def _instant(value: object) -> datetime:
    """Parse an ISO 8601 instant with a zone into aware UTC.

    Raises:
        ValueError: If the value is not a string or has no time zone.

    """
    if not isinstance(value, str):
        message = f"bad instant {value!r}"
        raise ValueError(message)  # noqa: TRY004 -- one error type for any bad note
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        message = f"instant without a time zone {value!r}"
        raise ValueError(message)
    return instant.astimezone(UTC)


def _optional_int(data: Mapping[str, object], key: str) -> int | None:
    """Return ``data[key]`` if it is an int or None (bools rejected)."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"bad {key} {value!r}"
        raise ValueError(message)  # noqa: TRY004 -- one error type for any bad note
    return value


def _parse_status(text: str) -> AttemptResult:
    """Turn the minder's JSON note into an ``AttemptResult``.

    Raises:
        ValueError: If the note is not the shape the minder writes.

    """
    raw: object = json.loads(text)
    if not isinstance(raw, dict):
        message = "not a JSON object"
        raise ValueError(message)  # noqa: TRY004 -- one error type for any bad note
    data = cast("dict[str, object]", raw)  # JSON object keys are always strings
    start_error = data.get("start_error")
    if start_error is not None and not isinstance(start_error, str):
        message = f"bad start_error {start_error!r}"
        raise ValueError(message)
    started_at = data.get("started_at")
    return AttemptResult(
        exit_code=_optional_int(data, "exit_code"),
        signal=_optional_int(data, "signal"),
        started_at=None if started_at is None else _instant(started_at),
        ended_at=_instant(data.get("ended_at")),
        start_error=start_error,
    )


def read_status(path: Path) -> AttemptResult | None:
    """Read the minder's status note at ``path``.

    Return None if there is no note. A note that exists but cannot be read
    or understood gives a result whose ``start_error`` begins
    ``corrupt status file:``; this never raises.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _corrupt(exc)
    try:
        return _parse_status(text)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError is a ValueError
        return _corrupt(exc)


def _corrupt(exc: Exception) -> AttemptResult:
    """Build the result for a status note that could not be understood."""
    return AttemptResult(
        exit_code=None,
        signal=None,
        started_at=None,
        ended_at=datetime.now(UTC),
        start_error=f"{CORRUPT_PREFIX}{exc}",
    )
