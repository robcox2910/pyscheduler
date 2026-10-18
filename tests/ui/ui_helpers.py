"""Helpers for the web UI tests: parse HTML, add a probe page, sign in, open event streams."""

from collections.abc import Mapping
from html.parser import HTMLParser

from api_helpers import PHRASE, ApiHarness
from pynet.loop import timeout
from pyweb.http1 import split_target
from pyweb.messages import Headers, HttpRequest, HttpResponse, QueryParams
from pyweb.sse import EventStream, ServerEvent
from pyweb.testing import TestResponse

from pyscheduler.api.auth import Principal
from pyscheduler.ui.html import h
from pyscheduler.ui.layout import page

HOSTILE = '<script>alert("x")</script>&'
"""Text that would run code or break markup if it reached a page unescaped."""

SHELL_PATH = "/shell"
"""A probe page the tests add: it renders the real shell around a small body."""

EVENT_WAIT = 5.0
"""The longest a test waits for one event (real seconds)."""

VOID = frozenset({"br", "hr", "input", "link", "meta"})

type Element = tuple[str, dict[str, str | None], str]


class _Collector(HTMLParser):
    """Collect every element as (tag, attributes, all the text inside it)."""

    def __init__(self) -> None:
        """Start with no elements."""
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[dict[str, str | None]] = []
        self.texts: list[str] = []
        self.open: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record the element; open it unless it is a void tag."""
        self.tags.append(tag)
        self.attrs.append(dict(attrs))
        self.texts.append("")
        if tag not in VOID:
            self.open.append(len(self.tags) - 1)

    def handle_endtag(self, tag: str) -> None:
        """Close the innermost open element with this tag."""
        for position in range(len(self.open) - 1, -1, -1):
            if self.tags[self.open[position]] == tag:
                del self.open[position:]
                return

    def handle_data(self, data: str) -> None:
        """Add text to every open element."""
        for index in self.open:
            self.texts[index] += data


def parse(text: str) -> list[Element]:
    """Return the page's elements as ``(tag, attrs, text)`` in document order."""
    collector = _Collector()
    collector.feed(text)
    return list(zip(collector.tags, collector.attrs, collector.texts, strict=True))


def find(text: str, tag: str, **attrs: str) -> list[Element]:
    """Return the elements with this tag whose attributes include ``attrs``."""
    return [
        element
        for element in parse(text)
        if element[0] == tag and all(element[1].get(k) == v for k, v in attrs.items())
    ]


def add_shell(h_: ApiHarness, *, name: str | None = None, body: str = "hello") -> None:
    """Add ``GET /shell``, which renders the shell around ``body`` (escaped).

    With ``name`` the page pretends that caller is logged in as that viewer
    (so a hostile name can reach the shell, which user files do not allow).
    """

    @h_.client.app.get(SHELL_PATH)
    async def shell(request: HttpRequest) -> HttpResponse:
        """Render the shell as the access layer's caller (or as ``name``)."""
        if name is not None:
            request.user = Principal(name, "viewer")
        return page(request, title=body, body=h("p", None, body), active="board")

    del shell


async def sign_in(
    h_: ApiHarness, name: str, password: str = PHRASE, next_path: str | None = None
) -> TestResponse:
    """Post the login form the way a browser does."""
    form = {"name": name, "password": password}
    if next_path is not None:
        form["next"] = next_path
    return await h_.client.post("/login", form=form)


def csrf_of(text: str) -> str:
    """Return the CSRF token in the page's ``<meta name="csrf-token">``."""
    (meta,) = find(text, "meta", name="csrf-token")
    return meta[1].get("content") or ""


async def open_stream(
    h_: ApiHarness, path: str, headers: Mapping[str, str] | None = None
) -> EventStream:
    """GET ``path`` through the app (with the client's cookies) and return its ``EventStream``.

    Unlike ``TestClient.stream`` this hands back the open stream, so a test
    can read one event, change something, and read the next.
    """
    target, pairs = split_target(path)
    sent = Headers([("Host", "testserver")])
    if h_.client.cookies:
        sent.set("Cookie", "; ".join(f"{k}={v}" for k, v in h_.client.cookies.items()))
    for name, value in (headers or {}).items():
        sent.set(name, value)
    outcome = await h_.client.app.handle(
        HttpRequest("GET", target, QueryParams(pairs), sent, b"", ("testclient", 0))
    )
    assert isinstance(outcome, EventStream), outcome.status
    return outcome


async def next_event(stream: EventStream) -> ServerEvent:
    """Return the stream's next event, failing the test if none comes within ``EVENT_WAIT``."""
    async with timeout(EVENT_WAIT):
        return await anext(stream.source)
