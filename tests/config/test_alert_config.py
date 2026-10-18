"""Tests for the alert config: ``notifiers:``, ``routes:`` and a job's ``alerts:``."""

import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from pyscheduler.config import Config, ConfigError, load_config
from pyscheduler.config.model import AlertKind, JobAlerts, NotifyRetry, Route

SLACK_URL = "https://hooks.example/abc"
SLACK_RETRIES = 3
SLACK_TIMEOUT = timedelta(seconds=5)
DEFAULT_TIMEOUT = timedelta(seconds=10)
MIN_PROBLEMS = 2

GOOD = """\
notifiers:
  team-log: {type: log, path: logs/alerts.log}
  ops-slack: {type: slack, webhook_url: "${SLACK_URL}", retries: {count: 3}, timeout: 5s}
routes:
  - {match: "alert.*.critical.#", notify: [ops-slack]}
  - {match: "alert.failed.#", notify: [team-log]}
jobs:
  load:
    command: "true"
    schedule: manual
    alerts: {on: [retrying], off: [too_short], notify: [team-log]}
  plain:
    command: "true"
    schedule: manual
"""


def _write(tmp_path: Path, text: str) -> Path:
    """Write ``text`` to ``scheduler.yaml`` under ``tmp_path``."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _load(tmp_path: Path, text: str) -> Config:
    """Load ``text`` with a Slack URL in the environment."""
    return load_config(_write(tmp_path, text), env={"SLACK_URL": SLACK_URL})


def _messages(tmp_path: Path, text: str, env: dict[str, str] | None = None) -> list[str]:
    """Return the messages of the ``ConfigError`` that loading ``text`` raises."""
    with pytest.raises(ConfigError) as info:
        load_config(_write(tmp_path, text), env=env or {})
    return [p.message for p in info.value.problems]


def _with_job(extra: str) -> str:
    """Return a config of one valid job plus the ``extra`` top-level text."""
    return extra + "jobs:\n  j:\n    command: x\n    schedule: manual\n"


class TestReading:
    """Read the sections into the typed model."""

    def test_reads_notifiers_and_routes(self, tmp_path: Path) -> None:
        """Load notifiers with expanded options, and the routes in order."""
        config = _load(tmp_path, GOOD)
        slack = config.notifiers["ops-slack"]
        assert slack.type == "slack"
        assert slack.options == {"webhook_url": SLACK_URL}
        assert slack.retries == NotifyRetry(count=SLACK_RETRIES)
        assert slack.timeout == SLACK_TIMEOUT
        assert config.notifiers["team-log"].options == {"path": "logs/alerts.log"}
        assert [(r.match, r.notify) for r in config.routes] == [
            ("alert.*.critical.#", ("ops-slack",)),
            ("alert.failed.#", ("team-log",)),
        ]
        assert isinstance(config.routes[0], Route)

    def test_notifier_defaults(self, tmp_path: Path) -> None:
        """Give a bare notifier the default retries and timeout."""
        log = _load(tmp_path, GOOD).notifiers["team-log"]
        assert log.retries == NotifyRetry()
        assert log.timeout == DEFAULT_TIMEOUT

    def test_job_alerts(self, tmp_path: Path) -> None:
        """Turn the written alerts into a ``JobAlerts`` with the default-off kind."""
        jobs = _load(tmp_path, GOOD).jobs
        load = jobs["load"].alerts
        assert load.enabled(AlertKind.RETRYING)
        assert not load.enabled(AlertKind.TOO_SHORT)
        assert load.notify == ("team-log",)
        plain = jobs["plain"].alerts
        assert plain == JobAlerts()
        assert not plain.enabled(AlertKind.RETRYING)
        assert plain.enabled(AlertKind.FAILED)

    def test_defaults_alerts_reach_every_job(self, tmp_path: Path) -> None:
        """Merge ``defaults: alerts:`` into every job."""
        text = "defaults:\n  alerts: {notify: [team-log]}\n" + GOOD
        jobs = _load(tmp_path, text).jobs
        assert jobs["plain"].alerts.notify == ("team-log",)
        assert jobs["load"].alerts.notify == ("team-log",)

    def test_on_wins_over_off(self) -> None:
        """Enable a kind that is both on and off."""
        both = JobAlerts(on=frozenset({AlertKind.FAILED}), off=frozenset({AlertKind.FAILED}))
        assert both.enabled(AlertKind.FAILED)


ERRORS = [
    ("notifiers:\n  n: {type: pager}\n", "unknown notifier type 'pager'"),
    ("notifiers:\n  n: {path: x}\n", "missing required key 'type'"),
    ("notifiers:\n  bad name: {type: log}\n", "invalid name 'bad name'"),
    ("notifiers:\n  n: {type: log, retries: {count: -1}}\n", "must be at least 0"),
    (
        "notifiers:\n  n: {type: log}\nroutes:\n  - {match: 'alert.#', notify: [nobody]}\n",
        "unknown notifier 'nobody'",
    ),
    (
        "notifiers:\n  n: {type: log}\nroutes:\n  - {match: 'alert..x', notify: [n]}\n",
        "invalid route pattern",
    ),
    (
        "notifiers:\n  n: {type: log}\nroutes:\n  - {match: 'alert.#'}\n",
        "missing required key 'notify'",
    ),
]


class TestErrors:
    """Report each alert config mistake once, with a clear message."""

    @pytest.mark.parametrize(("head", "expected"), ERRORS)
    def test_alert_config_errors(self, tmp_path: Path, head: str, expected: str) -> None:
        """Find exactly one problem containing ``expected``."""
        messages = _messages(tmp_path, _with_job(head))
        assert len(messages) == 1
        assert expected in messages[0]

    @pytest.mark.parametrize(
        ("alerts", "expected"),
        [
            ("{off: [late]}", "unknown alert kind 'late'"),
            ("{notify: [nobody]}", "unknown notifier 'nobody'"),
            ("{loud: true}", "unknown key 'loud'"),
            ("{on: failed}", "alerts.on must be a list"),
            ("{notify: [1]}", "alerts.notify items must be text"),
            ("{notify: [{a: b}]}", "alerts.notify items must be text"),
            ("{on: [[failed]]}", "alerts.on items must be text"),
        ],
    )
    def test_job_alert_errors(self, tmp_path: Path, alerts: str, expected: str) -> None:
        """Find exactly one problem in a job's ``alerts:``."""
        text = f"jobs:\n  j:\n    command: x\n    schedule: manual\n    alerts: {alerts}\n"
        messages = _messages(tmp_path, text)
        assert len(messages) == 1
        assert expected in messages[0]

    def test_reserved_job_name(self, tmp_path: Path) -> None:
        """Refuse the scheduler's own job name."""
        text = "jobs:\n  _scheduler:\n    command: x\n    schedule: manual\n"
        assert "job name '_scheduler' is reserved" in _messages(tmp_path, text)[0]

    def test_notifiers_only_in_main_file(self, tmp_path: Path) -> None:
        """Refuse ``notifiers:`` and ``routes:`` in an included file."""
        (tmp_path / "more.yaml").write_text(
            "notifiers:\n  n: {type: log}\nroutes: []\n", encoding="utf-8"
        )
        messages = _messages(tmp_path, "include: [more.yaml]\n" + _with_job(""))
        assert "notifiers is only allowed in the main file" in messages
        assert "routes is only allowed in the main file" in messages


class TestWarningsAndSecrets:
    """Warn about dead routes, and keep secrets out of messages."""

    @pytest.mark.parametrize("match", ["job.#", "alert.failed", "alert.a.b.c.d"])
    def test_route_pattern_warning(self, tmp_path: Path, match: str) -> None:
        """Load a route that can never match, with a warning."""
        text = _with_job(
            f"notifiers:\n  n: {{type: log}}\nroutes:\n  - {{match: '{match}', notify: [n]}}\n"
        )
        config = _load(tmp_path, text)
        [warning] = config.warnings
        assert "never matches an alert topic" in warning.message

    def test_live_route_has_no_warning(self, tmp_path: Path) -> None:
        """Do not warn about a route that can match."""
        assert _load(tmp_path, GOOD).warnings == ()

    def test_notifier_secret_is_redacted_in_errors(self, tmp_path: Path) -> None:
        """Show ``${SLACK_URL}`` and never the value in problem messages."""
        text = _with_job(
            "notifiers:\n"
            '  ops-slack: {type: slack, webhook_url: "${SLACK_URL}", timeout: nope,'
            ' retries: {count: "${SLACK_URL}"}}\n'
        )
        messages = _messages(tmp_path, text, {"SLACK_URL": SLACK_URL})
        assert len(messages) >= MIN_PROBLEMS
        assert not any(SLACK_URL in m for m in messages)
        assert any("${SLACK_URL}" in m for m in messages)
