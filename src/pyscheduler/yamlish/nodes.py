"""Node types produced by the YAML-subset reader.

Every node remembers the line it started on, so later stages can say
"jobs.yaml:12: unknown field" instead of a vague complaint.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scalar:
    """A piece of text, such as a name, a number or a command.

    Attributes:
        value: The text exactly as written, never converted to a number or bool.
        line: The 1-based line where the text starts.
        quoted: Whether the text was written inside quotes.

    """

    value: str
    line: int
    quoted: bool


@dataclass(frozen=True)
class Seq:
    """A list of nodes, written with dashes or square brackets."""

    items: list[Node]
    line: int


@dataclass(frozen=True)
class Map:
    """A set of named nodes, written as ``key: value`` lines or curly braces.

    Attributes:
        items: The values by key, in the order written.
        key_lines: The 1-based line of each key.
        line: The 1-based line of the first key.

    """

    items: dict[str, Node]
    key_lines: dict[str, int]
    line: int


type Node = Scalar | Seq | Map
