"""Safe HTML building: every value is escaped unless it was built here.

Imagine a tray of sealed sandwich boxes. Anything you hand the kitchen
(a job name, a log line) goes into a box with its wrapper on, so even a
name like ``<script>`` stays a harmless label. Only boxes the kitchen
itself sealed (``Safe``) go on the tray unwrapped.

``h(tag, attrs, *children)`` builds one element. Text children and
attribute values are escaped; ``Safe`` children (the result of another
``h`` or ``join``) pass through. Tags and attribute names come from a
fixed allow-list, so a hostile string can never become markup, and link
attributes refuse ``javascript:`` style addresses.
"""

import html
import re
from collections.abc import Mapping

__all__ = ["Safe", "h", "join"]


class Safe(str):
    """HTML that is already escaped (built by ``h`` or ``join``); never wrap untrusted text in it."""

    __slots__ = ()


TAGS = frozenset(
    {
        "a", "article", "aside", "b", "br", "button", "code", "dd", "details", "div", "dl",
        "dt", "em", "footer", "form", "h1", "h2", "h3", "h4", "header", "hr", "i", "input",
        "label", "li", "link", "main", "meta", "nav", "ol", "option", "p", "pre", "section",
        "select", "small", "span", "strong", "summary", "table", "tbody", "td", "textarea",
        "tfoot", "th", "thead", "time", "tr", "ul",
        "svg", "g", "rect", "line", "path", "text", "title", "polygon", "circle",
    }
)  # fmt: skip
"""The tags ``h`` may build."""

VOID_TAGS = frozenset({"br", "hr", "input", "link", "meta"})
"""Tags with no closing tag and no children."""

ATTRS = frozenset(
    {
        "action", "alt", "autocomplete", "autofocus", "checked", "class", "colspan", "content",
        "disabled", "hidden", "for", "height", "href", "id", "max", "maxlength", "method", "min", "name",
        "novalidate", "placeholder", "readonly", "rel", "required", "role", "rowspan", "scope",
        "selected", "step", "tabindex", "target", "title", "type", "value", "width",
        "datetime", "d", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry",
        "points", "fill", "stroke", "stroke-width", "stroke-dasharray", "viewBox",
        "transform", "text-anchor", "xmlns",
    }
)  # fmt: skip
"""The plain attributes ``h`` may set (``data-*`` and ``aria-*`` are allowed too)."""

_PREFIXED = re.compile(r"(?:data|aria)-[a-z][a-z0-9-]*")
_URL_ATTRS = frozenset({"href", "action"})
_SAFE_URL = re.compile(r"(?:/|#|\?|https?:|[^:/?#]*(?:[/?#]|$))", re.IGNORECASE)
_LOCAL = re.compile(r"(?:|/(?!/).*)")
_STRIP = re.compile(r"[\x00-\x20\x7f]+")


def _check_attr(tag: str, name: str, value: str) -> None:
    """Raise ``ValueError`` for an attribute name off the list or a link to a script."""
    if name not in ATTRS and not _PREFIXED.fullmatch(name):
        msg = f"attribute {name!r} is not allowed on <{tag}>"
        raise ValueError(msg)
    if name in _URL_ATTRS and ("\\" in value or _STRIP.sub("", value).startswith("//")):
        msg = f"{name}={value!r} may point off this site (backslash or //)"
        raise ValueError(msg)
    if name == "action" and not _LOCAL.fullmatch(_STRIP.sub("", value)):
        msg = f"action={value!r} must be a path on this site"
        raise ValueError(msg)
    if name in _URL_ATTRS and not _SAFE_URL.match(_STRIP.sub("", value)):
        msg = f"{name}={value!r} is not a local or http(s) address"
        raise ValueError(msg)


def _attrs(tag: str, attrs: Mapping[str, str | bool | None] | None) -> str:
    """Render ``attrs`` as `` name="value"`` text (``True`` bare, ``None``/``False`` omitted)."""
    parts: list[str] = []
    for name, value in (attrs or {}).items():
        if value is None or value is False:
            _check_attr(tag, name, "")
        elif value is True:
            _check_attr(tag, name, "")
            parts.append(f" {name}")
        else:
            _check_attr(tag, name, value)
            parts.append(f' {name}="{html.escape(value, quote=True)}"')
    return "".join(parts)


def h(
    tag: str, attrs: Mapping[str, str | bool | None] | None = None, *children: str | Safe | None
) -> Safe:
    """Return ``<tag attrs>children</tag>`` with text and attribute values escaped.

    Args:
        tag: A tag from ``TAGS``.
        attrs: Attributes: a string is escaped, ``True`` gives a bare
            attribute, ``None`` or ``False`` leaves it out.
        *children: Text (escaped), ``Safe`` HTML (kept) or ``None`` (skipped).

    Raises:
        ValueError: For a tag or attribute off the allow-lists, a
            ``javascript:`` style link, or children on a void tag.

    """
    if tag not in TAGS:
        msg = f"tag {tag!r} is not allowed"
        raise ValueError(msg)
    opening = f"<{tag}{_attrs(tag, attrs)}>"
    if tag in VOID_TAGS:
        if any(child is not None for child in children):
            msg = f"<{tag}> takes no children"
            raise ValueError(msg)
        return Safe(opening)
    inner = "".join(
        child if isinstance(child, Safe) else html.escape(child, quote=True)
        for child in children
        if child is not None
    )
    return Safe(f"{opening}{inner}</{tag}>")


def join(*parts: Safe | None) -> Safe:
    """Return the ``Safe`` parts one after another (``None`` is skipped)."""
    kept = [part for part in parts if part is not None]
    for part in kept:
        if not isinstance(part, Safe):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "join takes Safe parts (build text with h(), which escapes it)"
            raise TypeError(msg)
    return Safe("".join(kept))
