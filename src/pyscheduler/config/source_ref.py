"""Where a piece of config was written: the pin on the map for error messages."""

from dataclasses import dataclass

__all__ = ["SourceRef"]


@dataclass(frozen=True)
class SourceRef:
    """Where something was written: a file and a 1-based line."""

    file: str
    line: int
