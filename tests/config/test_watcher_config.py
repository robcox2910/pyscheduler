"""Tests for file-watcher config: the watch keys, ``{files}`` and ``watch_limit``."""

import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from pyscheduler.config import ConfigError, load_config
from pyscheduler.config.model import Config, JobKind

WATCHER = """\
jobs:
  trades-file:
    type: file_watcher
    watch: "/data/in/trades_{run_date:%Y%m%d}.csv"
    min_age: 30s
    poll: 10s
    give_up_after: 4h
    schedule: manual
  load:
    command: "cat {files}"
    depends_on: [trades-file]
"""
MIN_AGE = timedelta(seconds=30)
POLL = timedelta(seconds=10)
GIVE_UP = timedelta(hours=4)
DEFAULT_GIVE_UP = timedelta(days=1)
CUSTOM_LIMIT = 2
DEFAULT_LIMIT = 8


def _write(tmp_path: Path, text: str) -> Path:
    """Write ``text`` to ``scheduler.yaml`` under ``tmp_path``."""
    path = tmp_path / "scheduler.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _load(tmp_path: Path, text: str) -> Config:
    """Load ``text`` as a config."""
    return load_config(_write(tmp_path, text), env={})


def _messages(tmp_path: Path, text: str) -> list[str]:
    """Return the error messages from loading ``text``."""
    with pytest.raises(ConfigError) as info:
        load_config(_write(tmp_path, text), env={})
    return [p.message for p in info.value.problems]


def _watcher(extra: str) -> str:
    """Return a config with one watcher job carrying the extra indented lines."""
    return f"jobs:\n  w:\n    type: file_watcher\n    schedule: manual\n{extra}"


class TestReading:
    """Reading a watcher job."""

    def test_reads_watcher(self, tmp_path: Path) -> None:
        """Load the trades-file example."""
        job = _load(tmp_path, WATCHER).jobs["trades-file"]
        assert job.kind is JobKind.FILE_WATCHER
        assert job.command == ()
        assert job.watch is not None
        assert job.watch.pattern == "/data/in/trades_{run_date:%Y%m%d}.csv"
        assert (job.watch.min_age, job.watch.poll, job.watch.give_up_after) == (
            MIN_AGE,
            POLL,
            GIVE_UP,
        )

    def test_defaults_and_zero_min_age(self, tmp_path: Path) -> None:
        """Fill omitted keys with 30s, 10s and 1d; allow min_age of 0s."""
        plain = _load(tmp_path, _watcher("    watch: /x\n")).jobs["w"].watch
        assert plain is not None
        assert (plain.min_age, plain.poll, plain.give_up_after) == (MIN_AGE, POLL, DEFAULT_GIVE_UP)
        zero = _load(tmp_path, _watcher("    watch: /x\n    min_age: 0s\n")).jobs["w"].watch
        assert zero is not None
        assert zero.min_age == timedelta(0)

    def test_command_job_has_no_watch(self, tmp_path: Path) -> None:
        """Give an ordinary job the command kind."""
        job = _load(tmp_path, WATCHER).jobs["load"]
        assert job.kind is JobKind.COMMAND
        assert job.watch is None

    def test_defaults_supply_durations_and_are_ignored_otherwise(self, tmp_path: Path) -> None:
        """A watcher takes poll from defaults and ignores an inherited timeout."""
        text = "defaults: {poll: 5s, timeout: 1h}\n" + _watcher("    watch: /x\n")
        job = _load(tmp_path, text).jobs["w"]
        assert job.watch is not None
        assert job.watch.poll == timedelta(seconds=5)
        assert job.timeout is None


class TestErrors:
    """Mistakes in watcher config."""

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (_watcher(""), "missing required key 'watch'"),
            (_watcher("    watch: /x\n    command: ls\n"), "file watchers take no command"),
            (_watcher("    watch: /x\n    cwd: /tmp\n"), "file watchers take no cwd"),
            (_watcher("    watch: /x\n    env: {A: b}\n"), "file watchers take no env"),
            (_watcher("    watch: /x\n    timeout: 1h\n"), "file watchers take no timeout"),
            (_watcher("    watch: /x\n    pools: {p: 1}\n"), "file watchers take no pools"),
            (_watcher("    watch: /x\n    stdout: /o\n"), "file watchers take no stdout"),
            (_watcher("    watch: /x\n    stderr: /e\n"), "file watchers take no stderr"),
            (
                "jobs:\n  j:\n    command: ls\n    schedule: manual\n    watch: /x\n",
                "watch is only for file watchers",
            ),
            (
                _watcher("    watch: /x\n").replace("file_watcher", "cron"),
                "unknown job type 'cron'",
            ),
            (_watcher("    watch: '/x{attempt}'\n"), "unknown placeholder {attempt}"),
            (_watcher("    watch: '/x{files}'\n"), "unknown placeholder {files}"),
            (_watcher("    watch: /x\n    poll: 0s\n"), "must be greater than zero"),
            (_watcher("    watch: /x\n    give_up_after: 0s\n"), "must be greater than zero"),
            (
                "defaults: {type: file_watcher}\n" + _watcher("    watch: /x\n"),
                "type is not allowed",
            ),
            ("defaults: {watch: /x}\n" + _watcher("    watch: /x\n"), "watch is not allowed"),
        ],
    )
    def test_watcher_errors(self, tmp_path: Path, text: str, message: str) -> None:
        """Report each mistake with its message."""
        assert any(message in m for m in _messages(tmp_path, text))

    def test_bad_type_adds_no_missing_command(self, tmp_path: Path) -> None:
        """Do not pile a missing-command error on an unknown type."""
        text = "jobs:\n  j:\n    type: cron\n    schedule: manual\n"
        assert len(_messages(tmp_path, text)) == 1


class TestFilesPlaceholder:
    """The ``{files}`` placeholder."""

    def test_files_placeholder_only_for_direct_dependants(self, tmp_path: Path) -> None:
        """Allow ``{files}`` after a watcher and reject it elsewhere."""
        assert _load(tmp_path, WATCHER).jobs["load"].depends_on[0].job == "trades-file"
        other = WATCHER + '  other:\n    command: "cat {files}"\n    schedule: manual\n'
        assert any("unknown placeholder {files}" in m for m in _messages(tmp_path, other))
        indirect = WATCHER + '  later:\n    command: "cat {files}"\n    depends_on: [load]\n'
        assert any("unknown placeholder {files}" in m for m in _messages(tmp_path, indirect))


class TestWatchLimit:
    """The ``scheduler: watch_limit`` setting."""

    def test_watch_limit(self, tmp_path: Path) -> None:
        """Read the limit, default it to 8 and reject 0."""
        assert _load(tmp_path, WATCHER).settings.watch_limit == DEFAULT_LIMIT
        text = "scheduler: {watch_limit: 2}\n" + WATCHER
        assert _load(tmp_path, text).settings.watch_limit == CUSTOM_LIMIT
        low = "scheduler: {watch_limit: 0}\n" + WATCHER
        assert any("watch_limit must be at least 1" in m for m in _messages(tmp_path, low))


class TestDefinitionJson:
    """How a watcher shows in the API."""

    def test_definition_json(self, tmp_path: Path) -> None:
        """Show type and watch keys as seconds for watchers only."""
        from pyscheduler.api.serialize import definition_json  # noqa: PLC0415

        config = _load(tmp_path, WATCHER)
        watcher = definition_json(config, config.jobs["trades-file"], show_alerts=False)
        assert watcher["type"] == "file_watcher"
        assert watcher["watch"] == "/data/in/trades_{run_date:%Y%m%d}.csv"
        assert watcher["min_age_seconds"] == MIN_AGE.total_seconds()
        assert watcher["poll_seconds"] == POLL.total_seconds()
        assert watcher["give_up_after_seconds"] == GIVE_UP.total_seconds()
        command = definition_json(config, config.jobs["load"], show_alerts=False)
        assert command["type"] == "command"
        assert "watch" not in command


class TestSingleErrors:
    """Each mistake is reported once."""

    def test_non_scalar_min_age(self, tmp_path: Path) -> None:
        """Report a list min_age once."""
        found = _messages(tmp_path, _watcher("    watch: /x\n    min_age: [1]\n"))
        assert len([m for m in found if "min_age" in m]) == 1

    def test_misspelt_watch(self, tmp_path: Path) -> None:
        """Suggest the right key without also complaining it is missing."""
        found = _messages(tmp_path, _watcher("    wach: /x\n"))
        assert any("did you mean 'watch'" in m for m in found)
        assert not any("missing required key" in m for m in found)
