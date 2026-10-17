"""Quoted scalars, comments and flow collections for the YAML-subset reader.

These helpers work on plain strings and report problems as ``ScanError``
with an offset; the parser turns those into line-numbered ``YamlError``s.
"""

import re
import string

from pyscheduler.yamlish.nodes import Map, Node, Scalar, Seq

INDICATOR_MESSAGES = {
    "&": "anchors are not supported",
    "*": "aliases are not supported",
    "!": "tags are not supported",
}

_QUOTES = "\"'"
_OPENERS = " \t[{,"
_FLOW_SPACE = " \t\n\r"
_FLOW_STOP = ",]}"
_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "b": "\b",
    "f": "\f",
    "e": "\x1b",
    '"': '"',
    "\\": "\\",
    "/": "/",
    " ": " ",
}
_HEX_WIDTHS = {"x": 2, "u": 4, "U": 8}


class ScanError(Exception):
    """Describe a problem at an offset in a piece of text.

    Attributes:
        message: What is wrong.
        offset: Where in the scanned text it is wrong.
        incomplete: Whether more text might fix it (an unclosed bracket or quote).

    """

    def __init__(self, message: str, offset: int, *, incomplete: bool = False) -> None:
        """Remember the message, the offset and whether more text could help."""
        super().__init__(message)
        self.message = message
        self.offset = offset
        self.incomplete = incomplete


def parse_quoted(text: str, start: int) -> tuple[str, int]:
    """Read the quoted scalar that opens at ``text[start]``.

    Returns:
        The decoded text and the offset just past the closing quote.

    Raises:
        ScanError: If the quote is never closed or an escape is invalid.

    """
    if text[start] == "'":
        return _parse_single(text, start)
    return _parse_double(text, start)


def _parse_single(text: str, start: int) -> tuple[str, int]:
    """Read a single-quoted scalar, where only ``''`` is special."""
    out: list[str] = []
    i = start + 1
    while i < len(text):
        if text[i] != "'":
            out.append(text[i])
            i += 1
        elif text[i + 1 : i + 2] == "'":
            out.append("'")
            i += 2
        else:
            return "".join(out), i + 1
    msg = "unterminated quoted scalar"
    raise ScanError(msg, start, incomplete=True)


def _parse_double(text: str, start: int) -> tuple[str, int]:
    """Read a double-quoted scalar, decoding backslash escapes."""
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(text):
            break
        decoded, i = _decode_escape(text, i)
        out.append(decoded)
    msg = "unterminated quoted scalar"
    raise ScanError(msg, start, incomplete=True)


def _decode_escape(text: str, at: int) -> tuple[str, int]:
    """Decode the escape whose backslash is at ``text[at]``."""
    code = text[at + 1]
    if code in _ESCAPES:
        return _ESCAPES[code], at + 2
    width = _HEX_WIDTHS.get(code)
    digits = text[at + 2 : at + 2 + (width or 0)]
    if width and len(digits) == width and all(d in string.hexdigits for d in digits):
        try:
            return chr(int(digits, 16)), at + 2 + width
        except ValueError, OverflowError:
            pass
    msg = f"invalid escape '\\{code}'"
    raise ScanError(msg, at)


def strip_comment(text: str) -> str:
    """Return ``text`` without a trailing ``# comment`` (quotes protect hashes)."""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _QUOTES and (i == 0 or text[i - 1] in _OPENERS):
            try:
                i = parse_quoted(text, i)[1]
            except ScanError:
                return text
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        else:
            i += 1
    return text


def parse_flow(text: str, base_line: int) -> Node:
    """Parse a flow collection (``[...]`` or ``{...}``) that starts ``text``.

    Args:
        text: The collection, possibly spanning several lines.
        base_line: The line number of the first character of ``text``.

    Raises:
        ScanError: If the collection is malformed or not yet closed.

    """
    reader = _FlowReader(text, base_line)
    node = reader.value()
    reader.skip_space()
    if reader.at < len(text):
        msg = "unexpected text after flow collection"
        raise ScanError(msg, reader.at)
    return node


class _FlowReader:
    """Walk a flow collection one character at a time."""

    def __init__(self, text: str, base_line: int) -> None:
        """Start reading ``text`` at its first character."""
        self.text = text
        self.base_line = base_line
        self.at = 0

    def line_at(self, offset: int) -> int:
        """Return the line number of ``offset``."""
        return self.base_line + self.text.count("\n", 0, offset)

    def skip_space(self) -> None:
        """Move past spaces and newlines."""
        while self.at < len(self.text) and self.text[self.at] in _FLOW_SPACE:
            self.at += 1

    def value(self) -> Node:
        """Read one value of any kind."""
        self.skip_space()
        if self.at >= len(self.text):
            msg = "unterminated flow collection"
            raise ScanError(msg, 0, incomplete=True)
        ch = self.text[self.at]
        if ch == "[":
            return self.sequence()
        if ch == "{":
            return self.mapping()
        if ch in _QUOTES:
            start = self.at
            value, self.at = parse_quoted(self.text, start)
            return Scalar(value, self.line_at(start), quoted=True)
        return self.plain(is_key=False)

    def plain(self, *, is_key: bool) -> Scalar:
        """Read an unquoted scalar up to the next delimiter."""
        start = self.at
        while self.at < len(self.text):
            ch = self.text[self.at]
            if ch in _FLOW_STOP or (is_key and ch == ":" and self._colon_ends_key()):
                break
            self.at += 1
        raw = re.sub(r"\s*\n\s*", " ", self.text[start : self.at]).strip()
        if not raw:
            found = self.text[self.at] if self.at < len(self.text) else "end of text"
            msg = f"unexpected {found!r}"
            raise ScanError(msg, self.at)
        if raw[0] in INDICATOR_MESSAGES:
            raise ScanError(INDICATOR_MESSAGES[raw[0]], start)
        return Scalar(raw, self.line_at(start), quoted=False)

    def _colon_ends_key(self) -> bool:
        """Say whether the colon at the cursor separates a key from its value."""
        after = self.text[self.at + 1 : self.at + 2]
        return after == "" or after in _FLOW_SPACE or after in _FLOW_STOP

    def sequence(self) -> Seq:
        """Read ``[a, b, c]``."""
        start = self.at
        self.at += 1
        items: list[Node] = []
        while not self._at_close("]", start):
            items.append(self.value())
            self._after_entry(start)
        return Seq(items, self.line_at(start))

    def mapping(self) -> Map:
        """Read ``{a: 1, b: 2}``."""
        start = self.at
        self.at += 1
        items: dict[str, Node] = {}
        key_lines: dict[str, int] = {}
        while not self._at_close("}", start):
            self.skip_space()
            key_at = self.at
            key = self._key(start)
            if key in items:
                msg = f"duplicate key '{key}'"
                raise ScanError(msg, key_at)
            items[key] = self._map_value(key_at, start)
            key_lines[key] = self.line_at(key_at)
            self._after_entry(start)
        return Map(items, key_lines, self.line_at(start))

    def _key(self, start: int) -> str:
        """Read a quoted or plain key."""
        if self.at >= len(self.text):
            msg = "unterminated flow collection"
            raise ScanError(msg, start, incomplete=True)
        if self.text[self.at] in _QUOTES:
            key, self.at = parse_quoted(self.text, self.at)
            return key
        return self.plain(is_key=True).value

    def _map_value(self, key_at: int, start: int) -> Node:
        """Read what follows a key: ``: value``, or nothing."""
        self.skip_space()
        if self.at >= len(self.text):
            msg = "unterminated flow collection"
            raise ScanError(msg, start, incomplete=True)
        if self.text[self.at] != ":":
            return Scalar("", self.line_at(key_at), quoted=False)
        self.at += 1
        self.skip_space()
        if self.at < len(self.text) and self.text[self.at] in ",}":
            return Scalar("", self.line_at(key_at), quoted=False)
        return self.value()

    def _at_close(self, closer: str, start: int) -> bool:
        """Consume ``closer`` if it is next; fail if the text has run out."""
        self.skip_space()
        if self.at >= len(self.text):
            msg = "unterminated flow collection"
            raise ScanError(msg, start, incomplete=True)
        if self.text[self.at] == closer:
            self.at += 1
            return True
        return False

    def _after_entry(self, start: int) -> None:
        """Consume the comma after an entry, or leave the closer for the loop."""
        self.skip_space()
        if self.at >= len(self.text):
            msg = "unterminated flow collection"
            raise ScanError(msg, start, incomplete=True)
        if self.text[self.at] == ",":
            self.at += 1
        elif self.text[self.at] not in "]}":
            msg = f"unexpected {self.text[self.at]!r}"
            raise ScanError(msg, self.at)
