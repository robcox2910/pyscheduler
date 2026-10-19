"""The admin pages: config versions, a config's source and diff, and dead letters.

This is the **office behind the front desk**. Only admins get in (anyone
else is told "403" on a page of their own). ``/admin`` lists every saved
config version, newest first, has the reload button, and shows the
dead-letter tray: alerts that could not be delivered, each with a replay
button. ``/admin/configs/<version>`` shows that version's files as they
were saved (``${NAME}`` placeholders stay as typed, never filled in) and
what changed since the version before.

The dead-letter tray is also served alone at ``/ui/fragments/dead-letters``,
so app.js can redraw it after a replay.
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pynet.loop import run_in_thread
from pynet.messaging import JsonValue
from pyweb.app import App
from pyweb.messages import HttpRequest, HttpResponse, html

from pyscheduler.api.alerts import dead_rows
from pyscheduler.api.auth import allows
from pyscheduler.api.support import role_of
from pyscheduler.engine.core import Engine
from pyscheduler.store.config_reads import ConfigVersion
from pyscheduler.ui.config_diff import diff_view, sources_of
from pyscheduler.ui.html import Safe, h, join
from pyscheduler.ui.layout import page, secure

__all__ = ["DEAD_FRAGMENT", "add_admin"]

DEAD_FRAGMENT = "/ui/fragments/dead-letters"
DEAD_LIMIT = 200
"""The most dead deliveries the tray shows."""
DIGEST_SHOWN = 12
_FORBIDDEN = 403
_NOT_FOUND = 404
_DASH = "\N{EN DASH}"
type Row = dict[str, JsonValue]


def _local(moment: datetime, zone: ZoneInfo) -> str:
    """Return ``moment`` as ``YYYY-MM-DD HH:MM:SS`` in the scheduler's zone."""
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")


def _text(value: JsonValue) -> str:
    """Return a JSON value as display text (``-`` for nothing)."""
    return _DASH if value is None else str(value)


def _version_row(entry: ConfigVersion, current: int, zone: ZoneInfo) -> Safe:
    """Return one row of the versions table."""
    label = f"{entry.version} (running)" if entry.version == current else str(entry.version)
    return h(
        "tr",
        {"data-version": str(entry.version)},
        h("td", None, h("a", {"href": f"/admin/configs/{entry.version}"}, label)),
        h("td", None, _local(entry.loaded_at, zone)),
        h("td", None, h("code", None, entry.digest[:DIGEST_SHOWN])),
    )


def _versions(found: list[ConfigVersion], current: int, zone: ZoneInfo) -> Safe:
    """Return the versions table (newest first), or a note when none are stored."""
    if not found:
        return h("p", {"class": "empty"}, "No config versions are stored.")
    heads = ("Version", "Loaded", "Digest")
    return h(
        "table",
        {"class": "versions"},
        h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, t) for t in heads))),
        h("tbody", None, *(_version_row(e, current, zone) for e in found)),
    )


def _dead_row(row: Row, zone: ZoneInfo) -> Safe:
    """Return one dead delivery as a table row with its replay button."""
    alert = row["alert"] if isinstance(row["alert"], dict) else {}
    alert_id, notifier = str(alert.get("alert_id")), str(row["notifier"])
    updated = row["updated_at"]
    when = _DASH
    if isinstance(updated, str):
        when = _local(datetime.fromisoformat(updated), zone)
    run_id = alert.get("run_id")
    about = f"{alert.get('kind')}: {alert.get('message')}"
    return h(
        "tr",
        {"data-alert": alert_id, "data-notifier": notifier},
        h(
            "td",
            None,
            h("code", None, alert_id),
            h("br"),
            h("a", {"href": f"/runs/{quote(run_id, safe='')}"}, about)
            if isinstance(run_id, str)
            else about,
        ),
        h("td", None, notifier),
        h("td", None, _text(row["attempts"])),
        h("td", {"class": "last-error"}, _text(row["last_error"])),
        h("td", None, when),
        h(
            "td",
            None,
            h(
                "button",
                {
                    "type": "button",
                    "data-replay": alert_id,
                    "data-notifier": notifier,
                },
                "Replay",
            ),
        ),
    )


def dead_tray(rows: list[JsonValue], zone: ZoneInfo) -> Safe:
    """Return the dead-letter tray (the element app.js swaps after a replay)."""
    if not rows:
        inner = h("p", {"class": "empty"}, "No dead letters.")
    else:
        heads = ("Alert", "Notifier", "Attempts", "Last error", "Updated", "")
        shown = [_dead_row(r, zone) for r in rows if isinstance(r, dict)]
        inner = h(
            "table",
            {"class": "dead"},
            h("thead", None, h("tr", None, *(h("th", {"scope": "col"}, t) for t in heads))),
            h("tbody", None, *shown),
        )
    return h("div", {"id": "dead-letters"}, inner)


def _forbidden(request: HttpRequest) -> HttpResponse:
    """Return the 403 page for a caller who is not an admin."""
    body = h("p", {"class": "error", "role": "alert"}, "The admin pages need the admin role.")
    response = page(request, title="Admin", body=body, active="admin")
    response.status = _FORBIDDEN
    return response


def _missing(request: HttpRequest, version: int) -> HttpResponse:
    """Return the 404 page for a config version that is not stored."""
    body = h("p", {"class": "error", "role": "alert"}, f"unknown config version {version}")
    response = page(request, title="Config", body=body, active="admin")
    response.status = _NOT_FOUND
    return response


def _source_view(sources: dict[str, str]) -> Safe:
    """Return each file's text, shown as written (placeholders are not filled in)."""
    parts = [
        join(
            h("h3", None, Path(path).name),
            h("pre", {"class": "source", "data-file": Path(path).name}, sources[path]),
        )
        for path in sorted(sources, key=lambda p: (Path(p).name, p))
    ]
    return join(*parts) if parts else h("p", {"class": "empty"}, "This version holds no files.")


def add_admin(app: App, engine: Engine) -> None:
    """Add ``/admin``, ``/admin/configs/<version>`` and the dead-letter fragment to ``app``."""

    @app.get("/admin")
    async def admin(request: HttpRequest) -> HttpResponse:
        """Show the versions, the reload button and the dead letters."""
        if not allows(role_of(request), "admin"):
            return _forbidden(request)
        zone = engine.config.settings.timezone
        store = engine.store
        found = await run_in_thread(store.config_versions)
        rows = await run_in_thread(dead_rows, engine, DEAD_LIMIT)
        body = join(
            h("h1", None, "Admin"),
            h(
                "p",
                {"class": "admin-actions"},
                h("button", {"type": "button", "id": "reload-config"}, "Reload config"),
            ),
            h("p", {"id": "admin-message", "class": "error", "role": "status", "hidden": True}),
            h("h2", None, "Config versions"),
            _versions(found, engine.config_version, zone),
            h("h2", None, "Dead letters"),
            dead_tray(rows, zone),
        )
        return page(request, title="Admin", body=body, active="admin")

    @app.get("/admin/configs/<int:version>")
    async def config_page(request: HttpRequest, version: int) -> HttpResponse:
        """Show one version's files and what changed since the version before."""
        if not allows(role_of(request), "admin"):
            return _forbidden(request)
        zone = engine.config.settings.timezone
        store = engine.store
        record = await run_in_thread(store.get_config, version)
        if record is None:
            return _missing(request, version)
        before = await run_in_thread(store.get_config, version - 1)
        sources = sources_of(record.sources_json)
        if version <= 1:
            changes = h("p", {"class": "empty"}, "This is the first version.")
        elif before is None:
            changes = h("p", {"class": "empty"}, f"Version {version - 1} is no longer stored.")
        else:
            old = sources_of(before.sources_json)
            changes = diff_view(old, sources, f"v{version - 1}", f"v{version}")
        body = join(
            h("h1", None, f"Config version {version}"),
            h("p", None, h("a", {"href": "/admin"}, "Back to admin")),
            h(
                "p",
                None,
                f"Loaded {_local(record.loaded_at, zone)}, digest ",
                h("code", None, record.digest[:DIGEST_SHOWN]),
            ),
            h("h2", None, "Changes since the version before"),
            changes,
            h("h2", None, "Files"),
            _source_view(sources),
        )
        return page(request, title=f"Config {version}", body=body, active="admin")

    @app.get(DEAD_FRAGMENT)
    async def dead_letters(request: HttpRequest) -> HttpResponse:
        """Draw the dead-letter tray alone (403 for a caller who is not an admin)."""
        if not allows(role_of(request), "admin"):
            return secure(html(h("p", None, "forbidden"), _FORBIDDEN))
        rows = await run_in_thread(dead_rows, engine, DEAD_LIMIT)
        return secure(html(dead_tray(rows, engine.config.settings.timezone)))

    del admin, config_page, dead_letters
