"""Test the admin pages: the role gate, versions, the diff, dead letters and the sources."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from alert_helpers import make_alert
from api_helpers import START, ApiHarness, MakeApi, log_in, users_yaml
from ui_helpers import HOSTILE, find, sign_in

from pyscheduler.alerts.model import DeliveryRecord, DeliveryStatus
from pyscheduler.store import Batch
from pyscheduler.store.schema import ConfigRecord

OK = 200
FORBIDDEN = 403
NOT_FOUND = 404
SECRET = "xoxb-very-secret-value"  # noqa: S105 -- a test value
LATER = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)

CONFIG = """
notifiers:
  hook: {type: webhook, url: "https://example.org/${SLACK_URL}"}
jobs:
  etl:
    command: run-etl
    schedule: manual
"""


def _site(api: MakeApi) -> ApiHarness:
    """Return a harness with an admin, an operator and a viewer, and the secret in the env."""
    return api(
        CONFIG,
        env={"SLACK_URL": SECRET},
        tokens=False,
        users=users_yaml(ada="admin", ola="operator", vic="viewer"),
    )


async def _get(h_: ApiHarness, path: str, who: str | None = "ada") -> Any:
    """GET ``path`` signed in as ``who`` (a fresh session each time)."""
    h_.client.cookies.clear()
    if who is not None:
        await sign_in(h_, who)
    return await h_.client.get(path)


def _save(h_: ApiHarness, version: int, files: dict[str, str]) -> None:
    """Store a config version holding these file texts."""
    h_.store.apply(
        Batch(configs=[ConfigRecord(version, LATER, f"digest{version}", json.dumps(files))])
    )


def _dead(h_: ApiHarness, error: str = "boom", message: str = "load failed") -> str:
    """Store an alert with a dead delivery to ``hook``; return the alert id."""
    alert = replace(make_alert(), message=message)
    row = DeliveryRecord(alert.alert_id, "hook", DeliveryStatus.DEAD, 3, error, START)
    h_.store.apply(Batch(alerts=[alert], deliveries=[row]))
    return alert.alert_id


class TestRoleGate:
    """Only admins may open the admin pages and the dead-letter fragment."""

    def test_admin_needs_admin(self, api: MakeApi) -> None:
        """Viewers and operators get a 403 page; an admin gets 200."""
        h_ = _site(api)
        _save(h_, 1, {"/x/a.yaml": "jobs: {}\n"})
        paths = ("/admin", "/admin/configs/1", "/ui/fragments/dead-letters")

        async def body() -> None:
            """Ask for each page as each kind of user."""
            for path in paths:
                for who in ("vic", "ola"):
                    response = await _get(h_, path, who)
                    assert response.status == FORBIDDEN, (path, who)
                    assert "etl" not in response.text
                assert (await _get(h_, path, "ada")).status == OK, path
            denied = await _get(h_, "/admin", "ola")
            assert find(denied.text, "p", role="alert")
            assert not find(denied.text, "a", href="/admin")

        h_.run(body)


class TestVersions:
    """The versions list and the per-version page with its diff."""

    def test_versions_and_diff(self, api: MakeApi) -> None:
        """Two versions give -/+ lines; version 1 says first; an unknown one is 404."""
        h_ = _site(api)
        _save(
            h_,
            1,
            {"/srv/a/jobs.yaml": "jobs:\n  etl: one\n  keep: same\n", "/srv/old.yaml": "gone\n"},
        )
        _save(
            h_,
            2,
            {"/srv/a/jobs.yaml": "jobs:\n  etl: two\n  keep: same\n", "/srv/new.yaml": "fresh\n"},
        )

        async def body() -> None:
            """Open the list, version 2 (with a diff) and version 1."""
            listed = (await _get(h_, "/admin")).text
            rows = [r[1]["data-version"] for r in find(listed, "tr") if "data-version" in r[1]]
            assert rows == ["2", "1"]
            assert "digest2" in listed
            second = (await _get(h_, "/admin/configs/2")).text
            assert "/srv" not in second
            removed = [e[2] for e in find(second, "span", **{"class": "diff-del"})]
            added = [e[2] for e in find(second, "span", **{"class": "diff-add"})]
            assert "-  etl: one" in removed
            assert "+  etl: two" in added
            assert "-gone" in removed
            assert "+fresh" in added
            assert find(second, "span", **{"class": "diff-hunk"})
            assert find(second, "pre", **{"data-file": "jobs.yaml", "class": "source"})
            first = (await _get(h_, "/admin/configs/1")).text
            assert "first version" in first
            assert not find(first, "pre", **{"class": "diff"})
            assert (await _get(h_, "/admin/configs/99")).status == NOT_FOUND

        h_.run(body)

    def test_unchanged_file(self, api: MakeApi) -> None:
        """A file that did not change is named as unchanged, not diffed."""
        h_ = _site(api)
        _save(h_, 1, {"/x/a.yaml": "same\n"})
        _save(h_, 2, {"/x/a.yaml": "same\n"})

        async def body() -> None:
            """Open version 2."""
            text = (await _get(h_, "/admin/configs/2")).text
            assert "a.yaml: unchanged" in text

        h_.run(body)

    def test_diff_lines_are_single_spaced(self, api: MakeApi) -> None:
        """Each diff line is an inline span followed by one newline, so the block is not double-spaced."""
        h_ = _site(api)
        _save(h_, 1, {"/x/a.yaml": "one\ntwo\n"})
        _save(h_, 2, {"/x/a.yaml": "one\nTWO\n"})

        async def body() -> None:
            """Open version 2 and look at the markup and the stylesheet."""
            text = (await _get(h_, "/admin/configs/2")).text
            start = text.index('<pre class="diff"')
            block = text[start : text.index("</pre>", start)]
            spans = block.count("<span")
            assert spans == block.count("</span>\n")
            assert "\n\n" not in block
            assert "</span><span" not in block
            sheet = (await _get(h_, "/static/app.css")).text
            rule = next(r for r in sheet.splitlines() if r.startswith(".diff span"))
            assert "display: block" not in rule

        h_.run(body)

    def test_new_and_removed_empty_files(self, api: MakeApi) -> None:
        """An empty file that appeared or vanished says so, and is not "unchanged"."""
        h_ = _site(api)
        _save(h_, 1, {"/x/gone.yaml": "", "/x/same.yaml": ""})
        _save(h_, 2, {"/x/fresh.yaml": "", "/x/same.yaml": ""})

        async def body() -> None:
            """Open version 2."""
            text = (await _get(h_, "/admin/configs/2")).text
            assert "fresh.yaml: (new file)" in text
            assert "gone.yaml: (removed)" in text
            assert "same.yaml: unchanged" in text

        h_.run(body)

    def test_secret_in_source_stays_placeholder(self, api: MakeApi) -> None:
        """A reload stores the raw text: ``${SLACK_URL}`` stays, the value is never stored or shown."""
        h_ = _site(api)
        csrf = log_in(h_, "ada")
        headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}

        async def body() -> None:
            """Reload through the API, then open the new version and the list."""
            text = h_.config_path.read_text(encoding="utf-8")
            h_.config_path.write_text(text + "# edited\n", encoding="utf-8")
            reloaded = await h_.client.post("/api/reload", headers=headers, json={})
            assert reloaded.status == OK, reloaded.text
            stored = h_.store.get_config(2)
            assert stored is not None
            assert "${SLACK_URL}" in stored.sources_json
            assert SECRET not in stored.sources_json
            page = await h_.client.get("/admin/configs/2")
            assert page.status == OK
            assert "${SLACK_URL}" in page.text
            assert SECRET not in page.text
            assert SECRET not in (await h_.client.get("/admin")).text

        h_.run(body)


class TestDeadLetters:
    """The dead-letter tray on the admin page and as a fragment."""

    def test_dead_letters_listed(self, api: MakeApi) -> None:
        """Dead deliveries appear with their facts and replay buttons; sent ones do not."""
        h_ = _site(api)
        alert_id = _dead(h_, error="timeout")
        fine = make_alert(attempt=2)
        h_.store.apply(
            Batch(
                alerts=[fine],
                deliveries=[
                    DeliveryRecord(fine.alert_id, "hook", DeliveryStatus.SENT, 1, "", START)
                ],
            )
        )

        async def body() -> None:
            """Open the page and the fragment."""
            for path in ("/admin", "/ui/fragments/dead-letters"):
                text = (await _get(h_, path)).text
                (row,) = [r for r in find(text, "tr") if r[1].get("data-alert")]
                assert row[1]["data-alert"] == alert_id
                for shown in ("hook", "timeout", "3", "load failed"):
                    assert shown in row[2]
                (button,) = find(text, "button", **{"data-replay": alert_id})
                assert button[1]["data-notifier"] == "hook"
                assert fine.alert_id not in text
            assert "reload-config" in (await _get(h_, "/admin")).text

        h_.run(body)

    def test_no_dead_letters(self, api: MakeApi) -> None:
        """An empty tray says so, and is still the swappable element."""
        h_ = _site(api)

        async def body() -> None:
            """Open the fragment."""
            text = (await _get(h_, "/ui/fragments/dead-letters")).text
            assert find(text, "div", id="dead-letters")
            assert "No dead letters" in text

        h_.run(body)

    def test_expanded_secret_in_error_is_hidden(self, api: MakeApi) -> None:
        """A delivery error that holds a config value shows the placeholder."""
        h_ = _site(api)
        _dead(h_, error=f"401 for {SECRET}", message=f"cannot reach {SECRET}")

        async def body() -> None:
            """Open the page."""
            text = (await _get(h_, "/admin")).text
            assert SECRET not in text
            assert "${SLACK_URL}" in text

        h_.run(body)


@pytest.mark.parametrize("hostile", [HOSTILE])
def test_hostile_text_is_escaped(api: MakeApi, hostile: str) -> None:
    """Hostile source text and delivery errors show escaped on both pages."""
    h_ = _site(api)
    _save(h_, 2, {"/x/a.yaml": f"{hostile}\n"})
    _dead(h_, error=hostile)

    async def body() -> None:
        """Open the pages and look for raw markup."""
        for path in ("/admin", "/admin/configs/2"):
            text = (await _get(h_, path)).text
            assert "<script>alert" not in text
            assert "&lt;script&gt;" in text

    h_.run(body)
