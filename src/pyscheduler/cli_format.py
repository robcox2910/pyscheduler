"""Turn API answers into the text the ``pysched`` CLI prints.

A table is a list of rows of text; ``table`` pads each column to its
widest cell so the columns line up, like a printed timetable.
"""

import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = ["RUN_HEADER", "detail", "duration", "elapsed", "run_rows", "table", "text", "width"]

type JsonObject = dict[str, Any]

GAP = "  "
"""The space between two columns."""

NONE = "-"
"""What an empty cell shows."""

_MINUTE = 60
_HOUR = 60 * _MINUTE


def text(value: object) -> str:
    """Return a cell's text: ``-`` for ``None`` or ``""``, else ``str(value)``."""
    if value is None or value == "":
        return NONE
    return str(value)


def width(cell: str) -> int:
    """Return how many terminal columns ``cell`` takes (wide East Asian characters take 2)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in cell)


def table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Return ``header`` and ``rows`` as aligned columns (no trailing spaces)."""
    cells = [list(header), *([text(cell) for cell in row] for row in rows)]
    widths = [max(width(row[i]) for row in cells) for i in range(len(header))]
    lines = [
        GAP.join(cell + " " * (size - width(cell)) for cell, size in zip(row, widths, strict=True))
        for row in cells
    ]
    return "\n".join(line.rstrip() for line in lines)


def duration(seconds: float | None) -> str:
    """Return a duration such as ``0.4s``, ``12s``, ``3m05s`` or ``1h02m``; ``-`` for ``None``."""
    if seconds is None:
        return NONE
    if seconds < _MINUTE:
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"  # noqa: PLR2004
    whole = int(seconds)
    if whole < _HOUR:
        return f"{whole // _MINUTE}m{whole % _MINUTE:02d}s"
    return f"{whole // _HOUR}h{whole % _HOUR // _MINUTE:02d}m"


def elapsed(started_at: str | None, now: datetime | None = None) -> float | None:
    """Return the seconds since the ISO instant ``started_at`` (``None`` if it is missing)."""
    if not started_at:
        return None
    start = datetime.fromisoformat(started_at)
    return max(0.0, ((now or datetime.now(UTC)) - start).total_seconds())


def run_rows(runs: Sequence[JsonObject]) -> list[list[object]]:
    """Return ``runs`` as rows of job, run id, run date, state, attempt, trigger, reason."""
    return [
        [r["job"], r["run_id"], r["run_date"], r["state"], r["attempt"], r["trigger"], r["reason"]]
        for r in runs
    ]


RUN_HEADER = ("JOB", "RUN", "RUN_DATE", "STATE", "ATTEMPT", "TRIGGER", "REASON")
"""The columns of ``run_rows``."""


def detail(answer: JsonObject) -> str:
    """Return a run's detail (``GET /api/runs/{id}``): its fields, attempts and events."""
    run: JsonObject = answer["run"]
    width = max(len(key) for key in run)
    lines = [f"{key.ljust(width)}  {text(value)}" for key, value in run.items()]
    attempts: list[JsonObject] = answer.get("attempts", [])
    lines += ["", "attempts:"]
    rows = [
        [
            a["attempt"],
            a["started_at"],
            a["ended_at"],
            duration(a["duration_seconds"]),
            a["outcome"],
            a["exit_code"],
            a["reason"],
        ]
        for a in attempts
    ]
    header = ("ATTEMPT", "STARTED", "ENDED", "DURATION", "OUTCOME", "EXIT", "REASON")
    lines.append(table(header, rows) if rows else "  (none)")
    events: list[JsonObject] = answer.get("events", [])
    lines += ["", "events:"]
    lines += [f"  {e['at']}  {e['actor']}  {e['action']}  {_detail(e['detail'])}" for e in events]
    if not events:
        lines.append("  (none)")
    return "\n".join(lines)


def _detail(value: object) -> str:
    """Return an event's detail as ``key=value`` pairs (or its text)."""
    if isinstance(value, dict):
        items: dict[str, object] = value  # pyright: ignore[reportUnknownVariableType]
        return " ".join(f"{k}={v}" for k, v in items.items())
    return text(value)
