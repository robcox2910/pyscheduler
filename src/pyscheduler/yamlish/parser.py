"""Turn YAML-subset text into a tree of nodes with line numbers.

The reader is a line-based recursive descent driven by indentation. It
supports block mappings and sequences, flow ``[...]`` and ``{...}``
collections, quoted scalars, ``|`` and ``>`` block scalars, comments and a
leading ``---``. Anchors, aliases, tags, complex keys, tabs in indentation
and multiple documents are rejected with a clear message.
"""

import re

from pyscheduler.yamlish._scalars import (
    INDICATOR_MESSAGES,
    ScanError,
    parse_flow,
    parse_quoted,
    strip_comment,
)
from pyscheduler.yamlish.nodes import Map, Node, Scalar, Seq

_BLOCK_HEADER = re.compile(r"^([|>])([+-]?)$")
_QUOTES = "\"'"
_DOC_MARKER = "---"


class YamlError(ValueError):
    """Describe a problem in a YAML document.

    Attributes:
        filename: The name used for the document in messages.
        line: The 1-based line of the problem.
        message: What is wrong.

    """

    def __init__(self, filename: str, line: int, message: str) -> None:
        """Remember where the problem is and what it is."""
        super().__init__(f"{filename}:{line}: {message}")
        self.filename = filename
        self.line = line
        self.message = message


def parse(text: str, filename: str = "<string>") -> Node:
    """Parse ``text`` into a node tree.

    Args:
        text: The YAML-subset document.
        filename: The name shown in error messages.

    Returns:
        The root node; an empty document gives an empty ``Map`` on line 1.

    Raises:
        YamlError: If the document is malformed or uses an unsupported feature.

    """
    return _Reader(text, filename).document()


def _is_dash(content: str) -> bool:
    """Say whether ``content`` starts a sequence item."""
    return content == "-" or content.startswith(("- ", "-\t"))


def _indent_of(line: str) -> int:
    """Count the leading spaces of ``line``."""
    return len(line) - len(line.lstrip(" "))


class _Reader:
    """Walk the lines of one document."""

    def __init__(self, text: str, filename: str) -> None:
        """Split ``text`` into lines, ready to read from the top."""
        self.filename = filename
        self.lines = [line.rstrip("\r") for line in text.split("\n")]
        self.pos = 0

    def error(self, line: int, message: str) -> YamlError:
        """Build a ``YamlError`` for ``line``."""
        return YamlError(self.filename, line, message)

    def scan_error(self, err: ScanError, line: int) -> YamlError:
        """Turn a ``ScanError`` found in text starting on ``line`` into a ``YamlError``."""
        return self.error(line, err.message)

    # -- navigation -------------------------------------------------------

    def peek(self, *, allow_marker: bool = False) -> tuple[int, str, int] | None:
        """Move to the next meaningful line; return its indent, content and number."""
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            lineno = self.pos + 1
            content = strip_comment(raw.lstrip(" \t")).rstrip()
            if not content:
                self.pos += 1
                continue
            indent = _indent_of(raw)
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                raise self.error(lineno, "tabs are not allowed in indentation")
            self._reject_unsupported(content, lineno, allow_marker=allow_marker)
            return indent, content, lineno
        return None

    def _reject_unsupported(self, content: str, lineno: int, *, allow_marker: bool) -> None:
        """Fail on document markers and complex keys."""
        if content.startswith(_DOC_MARKER) and content[3:4] in ("", " ", "\t"):
            if not (allow_marker and content == _DOC_MARKER):
                raise self.error(lineno, "multiple documents are not supported")
        elif content[0] == "?" and content[1:2] in ("", " ", "\t"):
            raise self.error(lineno, "complex keys ('? ') are not supported")

    # -- structure --------------------------------------------------------

    def document(self) -> Node:
        """Read the whole document."""
        first = self.peek(allow_marker=True)
        if first is not None and first[1] == _DOC_MARKER:
            self.pos += 1
        current = self.peek()
        if current is None:
            return Map({}, {}, 1)
        node = self.block(current[0])
        leftover = self.peek()
        if leftover is not None:
            raise self.error(leftover[2], "bad indentation")
        return node

    def block(self, indent: int) -> Node:
        """Read the block node whose first line is at ``indent``."""
        current = self.peek()
        if current is None:
            raise self.error(len(self.lines), "unexpected end of document")
        _, content, lineno = current
        if _is_dash(content):
            return self.sequence(indent)
        if self.split_key(content, lineno) is not None:
            return self.mapping(indent)
        self.pos += 1
        return self.value(content, lineno, indent - 1)

    def sequence(self, indent: int) -> Seq:
        """Read the ``- item`` lines at ``indent``."""
        items: list[Node] = []
        first_line = self.pos + 1
        while (current := self.peek()) is not None:
            line_indent, content, lineno = current
            if line_indent < indent or (line_indent == indent and not _is_dash(content)):
                break
            if line_indent > indent:
                raise self.error(lineno, "bad indentation")
            rest = content[1:].lstrip(" \t")
            column = indent + len(content) - len(rest)
            if not rest:
                self.pos += 1
                items.append(self.empty_value(indent, lineno, same_indent_sequence=False))
            elif rest[0] in "[{|>":
                self.pos += 1
                items.append(self.value(rest, lineno, indent))
            else:
                self.lines[self.pos] = " " * column + rest
                items.append(self.block(column))
        return Seq(items, first_line)

    def mapping(self, indent: int) -> Map:
        """Read the ``key: value`` lines at ``indent``."""
        items: dict[str, Node] = {}
        key_lines: dict[str, int] = {}
        first_line = self.pos + 1
        while (current := self.peek()) is not None:
            line_indent, content, lineno = current
            if line_indent < indent:
                break
            if line_indent > indent:
                raise self.error(lineno, "bad indentation")
            split = self.split_key(content, lineno)
            if split is None:
                raise self.error(lineno, "expected 'key: value'")
            key, quoted, rest = split
            if not quoted and key[0] in INDICATOR_MESSAGES:
                raise self.error(lineno, INDICATOR_MESSAGES[key[0]])
            if key in items:
                raise self.error(lineno, f"duplicate key '{key}'")
            self.pos += 1
            key_lines[key] = lineno
            if rest:
                items[key] = self.value(rest, lineno, indent)
            else:
                items[key] = self.empty_value(indent, lineno, same_indent_sequence=True)
        return Map(items, key_lines, first_line)

    def empty_value(self, parent_indent: int, lineno: int, *, same_indent_sequence: bool) -> Node:
        """Read what follows a bare ``key:`` or ``-``: a nested block or nothing."""
        current = self.peek()
        if current is not None:
            indent, content, _ = current
            if indent > parent_indent or (
                same_indent_sequence and indent == parent_indent and _is_dash(content)
            ):
                return self.block(indent)
        return Scalar("", lineno, quoted=False)

    # -- single values ----------------------------------------------------

    def split_key(self, content: str, lineno: int) -> tuple[str, bool, str] | None:
        """Split ``key: rest`` into the key, whether it was quoted, and the rest."""
        if content[0] in "[{|>" or _is_dash(content):
            return None
        if content[0] in _QUOTES:
            try:
                key, end = parse_quoted(content, 0)
            except ScanError as err:
                raise self.scan_error(err, lineno) from None
            after = content[end:].lstrip(" \t")
            if after[:1] == ":" and after[1:2] in ("", " ", "\t"):
                return key, True, after[1:].strip()
            return None
        for i, ch in enumerate(content):
            if ch == ":" and content[i + 1 : i + 2] in ("", " ", "\t"):
                key = content[:i].rstrip()
                return (key, False, content[i + 1 :].strip()) if key else None
        return None

    def value(self, rest: str, lineno: int, parent_indent: int) -> Node:
        """Read the value written after a key or dash (``rest`` is not empty)."""
        first = rest[0]
        if first in "|>":
            return self.block_scalar(rest, lineno, parent_indent)
        if first in "[{":
            return self.flow(rest, lineno)
        if first in _QUOTES:
            try:
                text, end = parse_quoted(rest, 0)
            except ScanError as err:
                raise self.scan_error(err, lineno) from None
            if rest[end:].strip():
                raise self.error(lineno, "unexpected text after quoted scalar")
            return Scalar(text, lineno, quoted=True)
        if first in INDICATOR_MESSAGES:
            raise self.error(lineno, INDICATOR_MESSAGES[first])
        return Scalar(rest, lineno, quoted=False)

    def flow(self, rest: str, lineno: int) -> Node:
        """Read a flow collection, pulling in more lines until it closes."""
        text = rest
        while True:
            try:
                return parse_flow(text, lineno)
            except ScanError as err:
                if not err.incomplete or self.pos >= len(self.lines):
                    line = lineno if err.incomplete else lineno + text.count("\n", 0, err.offset)
                    raise self.scan_error(err, line) from None
                text += "\n" + strip_comment(self.lines[self.pos])
                self.pos += 1

    def block_scalar(self, header: str, lineno: int, parent_indent: int) -> Scalar:
        """Read a ``|`` or ``>`` scalar whose lines are indented past ``parent_indent``."""
        match = _BLOCK_HEADER.match(header)
        if match is None:
            raise self.error(lineno, f"unsupported block scalar header '{header}'")
        style, chomp = match.groups()
        raw: list[str] = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.strip() and _indent_of(line) <= parent_indent:
                break
            raw.append(line)
            self.pos += 1
        body = self._dedent(raw, lineno)
        trailing = 0
        while body and not body[-1].strip():
            body.pop()
            trailing += 1
        text = "\n".join(body) if style == "|" else _fold(body)
        if chomp == "+":
            text += "\n" * (trailing + 1) if body else "\n" * trailing
        elif chomp == "" and body:
            text += "\n"
        return Scalar(text, lineno, quoted=False)

    def _dedent(self, raw: list[str], header_line: int) -> list[str]:
        """Remove the block's own indentation from each of its lines."""
        indents = [_indent_of(line) for line in raw if line.strip()]
        if not indents:
            return ["" for _ in raw]
        width = indents[0]
        for offset, line in enumerate(raw):
            if line.strip() and _indent_of(line) < width:
                raise self.error(header_line + 1 + offset, "bad indentation in block scalar")
        return [line[width:] if line.strip() else "" for line in raw]


def _fold(body: list[str]) -> str:
    """Join the lines of a ``>`` scalar: single breaks become spaces."""
    text = ""
    previous: str | None = None
    blanks = 0
    for line in body:
        if not line:
            blanks += 1
            continue
        if previous is None:
            text = "\n" * blanks + line
        elif line[0] in " \t" or previous[0] in " \t":
            text += "\n" * (blanks + 1) + line
        elif blanks:
            text += "\n" * blanks + line
        else:
            text += " " + line
        previous = line
        blanks = 0
    return text
