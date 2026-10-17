"""Tests for the config model and loader."""

import hashlib
import textwrap
from collections.abc import Callable
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pyscheduler.calendars import ALL_DAYS
from pyscheduler.config import (
    CatchUp,
    Config,
    ConfigError,
    Criticality,
    JobDef,
    Overlap,
    Problem,
    RetryPolicy,
    RunDateRule,
    SourceRef,
    build_config,
    load_config,
)

type WriteYaml = Callable[[str, str], Path]

FULL_EXAMPLE = """
scheduler:
  name: prod
  timezone: Europe/London        # default for jobs and calendars; default UTC
  state_dir: /var/lib/pysched    # PyDB lives here
  log_dir: /var/log/pysched      # default root for job logs
  max_running: 32                # global cap on running attempts
  listen: 127.0.0.1:8470         # HTTP API (and UI from phase 3)
  api_tokens_file: tokens.yaml   # bearer tokens for the API (see API)
  kill_grace: 10s                # SIGTERM -> SIGKILL wait

calendars:
  uk-business:
    weekdays: [mon, tue, wed, thu, fri]
    holidays: [2026-12-25, 2026-12-28, 2027-01-01]
    extra_days: []               # weekend days that count as business days
    include: [other-calendar]    # optional: union of other calendars' holidays
  other-calendar:
    holidays: [2026-08-31]

pools:
  database: 2                    # at most 2 running attempts hold this pool

defaults:                        # merged into every job (job keys win)
  retries: {count: 2, delay: 1m, backoff: 2, max_delay: 10m}
  criticality: normal

jobs:
  extract-trades:
    command: /opt/etl/extract.sh --date {run_date}
    schedule: "30 18 * * *"
    calendar: uk-business
    timeout: 2h
    pools: {database: 1}
    tags: [etl]
  load-warehouse:
    command: ["python", "-m", "etl.load", "--date", "{run_date}"]
    depends_on: [extract-trades]
    criticality: critical
"""

DEFAULT_RETRY_COUNT = 2
JOB_RETRY_COUNT = 5
POOL_SIZE = 2
LISTEN_PORT = 8470
MAX_RUNNING = 32
LINE_TWO = 2
TWO_PROBLEMS = 2
LINE_THREE = 3
LINE_FOUR = 4
LINE_FIVE = 5
LINE_SIX = 6
HOLIDAY_COUNT = 4


def _digest(files: list[tuple[str, str]]) -> str:
    """Return the expected digest: path, NUL, text, NUL for each file."""
    sha = hashlib.sha256()
    for name, text in files:
        sha.update(f"{name}\0{text}\0".encode())
    return sha.hexdigest()


def _job(name: str, body: str) -> str:
    """Return a ``jobs:`` document with one job whose keys are ``body`` lines."""
    lines = "".join(f"    {line}\n" for line in textwrap.dedent(body).strip().splitlines())
    return f"jobs:\n  {name}:\n{lines}"


def _ok(path: Path, env: dict[str, str] | None = None) -> Config:
    """Build a config and assert it has no problems."""
    config, problems = build_config(path, env=env or {})
    assert problems == []
    return config


def _errors(path: Path, env: dict[str, str] | None = None) -> list[Problem]:
    """Build a config and return its problems."""
    _, problems = build_config(path, env=env or {})
    return problems


class TestFullExample:
    """The spec's example config."""

    def test_full_example_loads(self, write_yaml: WriteYaml) -> None:
        """The example loads with the documented values."""
        path = write_yaml("scheduler.yaml", FULL_EXAMPLE)
        config = load_config(path, env={})
        warehouse = config.jobs["load-warehouse"]
        extract = config.jobs["extract-trades"]
        assert warehouse.criticality is Criticality.CRITICAL
        assert warehouse.retries.count == DEFAULT_RETRY_COUNT
        assert config.pools["database"] == POOL_SIZE
        assert isinstance(extract.command, str)
        assert warehouse.command == ("python", "-m", "etl.load", "--date", "{run_date}")
        assert extract.pools == {"database": 1}
        assert extract.timeout == timedelta(hours=2)
        assert extract.tags == ("etl",)
        assert extract.calendar == "uk-business"
        assert warehouse.calendar is None
        assert warehouse.depends_on[0].job == "extract-trades"
        assert warehouse.dependency_only
        assert not extract.dependency_only
        assert config.dependents("extract-trades") == frozenset({"load-warehouse"})
        assert config.dependents("load-warehouse") == frozenset()

    def test_settings(self, write_yaml: WriteYaml) -> None:
        """Scheduler settings are typed; relative paths sit next to the main file."""
        path = write_yaml("scheduler.yaml", FULL_EXAMPLE)
        settings = load_config(path, env={}).settings
        assert settings.name == "prod"
        assert settings.timezone == ZoneInfo("Europe/London")
        assert settings.state_dir == Path("/var/lib/pysched")
        assert settings.listen == ("127.0.0.1", LISTEN_PORT)
        assert settings.max_running == MAX_RUNNING
        assert settings.kill_grace == timedelta(seconds=10)
        assert settings.api_tokens_file == path.parent / "tokens.yaml"

    def test_calendars_and_digest(self, write_yaml: WriteYaml) -> None:
        """Calendars merge included holidays; the digest hashes the source."""
        path = write_yaml("scheduler.yaml", FULL_EXAMPLE)
        config = load_config(path, env={})
        uk = config.calendars["uk-business"]
        assert uk.weekdays == frozenset(range(5))
        assert len(uk.holidays) == HOLIDAY_COUNT  # three own plus one included
        assert config.calendar_for(config.jobs["extract-trades"]) is uk
        assert config.calendar_for(config.jobs["load-warehouse"]) is ALL_DAYS
        text = path.read_text(encoding="utf-8")
        assert config.digest == _digest([(str(path), text)])
        assert config.sources == {str(path): text}

    def test_job_defaults(self, write_yaml: WriteYaml) -> None:
        """A minimal job gets the documented defaults."""
        path = write_yaml("s.yaml", _job("j", "command: echo hi\nschedule: manual"))
        job = _ok(path).jobs["j"]
        assert job == JobDef(
            name="j",
            command="echo hi",
            cwd=None,
            env={},
            schedules=(),
            manual=True,
            calendar=None,
            timezone=ZoneInfo("UTC"),
            run_date_rule=RunDateRule.SAME_DAY,
            depends_on=(),
            retries=RetryPolicy(),
            timeout=None,
            criticality=Criticality.NORMAL,
            pools={},
            overlap=Overlap.QUEUE,
            catch_up=CatchUp.LATEST,
            stdout="{log_dir}/{job}/{run_date}/{run_id}.{attempt}.out",
            stderr="{log_dir}/{job}/{run_date}/{run_id}.{attempt}.err",
            min_runtime=None,
            max_runtime=None,
            must_start_by=None,
            alerts={},
            agent=None,
            agent_tags=(),
            tags=(),
            description="",
            owner="",
            enabled=True,
            source=SourceRef(str(path), LINE_TWO),
        )

    def test_all_job_keys(self, write_yaml: WriteYaml) -> None:
        """Every optional job key is read into its typed field."""
        body = """
        command: run
        cwd: /srv/work
        env: {A: "1"}
        schedule: "@daily"
        timezone: America/New_York
        run_date: previous_business_day
        overlap: skip
        catch_up: all
        stderr: stdout
        min_runtime: 1m
        max_runtime: 1h
        must_start_by: "07:30"
        alerts: {on: [failure], to: ops}
        agent: box1
        agent_tags: [linux]
        description: hello
        owner: me
        enabled: false
        """
        job = _ok(write_yaml("s.yaml", _job("j", body))).jobs["j"]
        assert job.cwd == "/srv/work"
        assert job.env == {"A": "1"}
        assert job.timezone == ZoneInfo("America/New_York")
        assert job.run_date_rule is RunDateRule.PREVIOUS_BUSINESS_DAY
        assert job.overlap is Overlap.SKIP
        assert job.catch_up is CatchUp.ALL
        assert job.stderr == "stdout"
        assert job.min_runtime == timedelta(minutes=1)
        assert job.max_runtime == timedelta(hours=1)
        assert job.must_start_by == time(7, 30)
        assert job.alerts == {"on": ["failure"], "to": "ops"}
        assert job.agent == "box1"
        assert job.agent_tags == ("linux",)
        assert (job.description, job.owner, job.enabled) == ("hello", "me", False)

    def test_must_start_by_duration(self, write_yaml: WriteYaml) -> None:
        """``must_start_by`` may be a duration after the scheduled time."""
        path = write_yaml("s.yaml", _job("j", "command: x\nschedule: manual\nmust_start_by: 30m"))
        assert _ok(path).jobs["j"].must_start_by == timedelta(minutes=30)


class TestMerging:
    """Defaults and includes."""

    def test_defaults_merge_job_wins(self, write_yaml: WriteYaml) -> None:
        """Nested defaults merge key by key and job keys win."""
        text = "defaults:\n  retries: {count: 2, delay: 1m}\n  overlap: skip\n" + _job(
            "j", "command: x\nschedule: manual\nretries: {count: 5}\noverlap: allow"
        )
        job = _ok(write_yaml("s.yaml", text)).jobs["j"]
        assert job.retries.count == JOB_RETRY_COUNT
        assert job.retries.delay == timedelta(minutes=1)
        assert job.overlap is Overlap.ALLOW

    def test_include_globs(self, write_yaml: WriteYaml) -> None:
        """Included files load in sorted order; duplicates name both places."""
        main = write_yaml("main.yaml", 'include: ["jobs/*.yaml"]\n')
        a = write_yaml("jobs/a.yaml", _job("one", "command: x\nschedule: manual"))
        b = write_yaml(
            "jobs/b.yaml",
            "jobs:\n  two:\n    command: y\n    schedule: manual\n"
            "  one:\n    command: z\n    schedule: manual\n",
        )
        config, problems = build_config(main, env={})
        assert set(config.jobs) == {"one", "two"}
        assert len(problems) == 1
        problem = problems[0]
        assert (problem.file, problem.line) == (str(b), LINE_FIVE)
        assert f"{a}:{LINE_TWO}" in problem.message
        files = [(str(p), p.read_text(encoding="utf-8")) for p in (main, a, b)]
        assert config.digest == _digest(files)
        assert list(config.sources) == [str(main), str(a), str(b)]

    def test_include_loop_reads_each_file_once(self, write_yaml: WriteYaml) -> None:
        """Files that include each other are each read once, without looping."""
        main = write_yaml("main.yaml", "include: [sub/*.yaml]\n")
        loop = write_yaml("sub/loop.yaml", 'include: [../main.yaml, "*.yaml"]\n')
        config = _ok(main)
        assert list(config.sources) == [str(main), str(loop)]

    def test_include_missing_file(self, write_yaml: WriteYaml) -> None:
        """An include naming a file that does not exist is a problem."""
        main = write_yaml("main.yaml", "include: [nope.yaml]\n")
        assert "matched no files" in _errors(main)[0].message

    def test_include_only_jobs(self, write_yaml: WriteYaml) -> None:
        """Scheduler settings and defaults belong in the main file only."""
        main = write_yaml("main.yaml", "include: [b.yaml]\n")
        write_yaml("b.yaml", "scheduler: {name: x}\n")
        assert "only allowed in the main file" in _errors(main)[0].message


class TestProblems:
    """Problems are collected, not raised one at a time."""

    def test_collects_all_problems(self, write_yaml: WriteYaml) -> None:
        """Three errors in one file give three problems on the right lines."""
        body = "comand: x\nschedule: manual\nretries: {count: lots}\ntimeout: 5x"
        problems = _errors(write_yaml("s.yaml", _job("j", body)))
        assert [p.line for p in problems] == [LINE_THREE, LINE_FIVE, LINE_SIX]
        assert "did you mean 'command'" in problems[0].message
        assert all(p.job == "j" for p in problems)

    def test_load_config_raises(self, write_yaml: WriteYaml) -> None:
        """``load_config`` raises with every problem."""
        path = write_yaml("s.yaml", _job("j", "command: x\ntimeout: 5x\nenabled: no"))
        with pytest.raises(ConfigError) as info:
            load_config(path, env={})
        assert len(info.value.problems) == TWO_PROBLEMS

    def test_bool_is_strict(self, write_yaml: WriteYaml) -> None:
        """Only ``true`` and ``false`` are booleans."""
        problems = _errors(write_yaml("s.yaml", _job("j", "command: x\nenabled: no")))
        assert len(problems) == 1
        assert "true or false" in problems[0].message

    def test_env_expansion(self, write_yaml: WriteYaml) -> None:
        """``${NAME}`` expands from the environment; a missing one is a problem."""
        path = write_yaml("s.yaml", _job("j", "command: ls ${HOME_DIR}\ncwd: ${NOPE}"))
        config, problems = build_config(path, env={"HOME_DIR": "/home/me"})
        assert config.jobs["j"].command == "ls /home/me"
        assert len(problems) == 1
        assert problems[0].line == LINE_FOUR
        assert "NOPE" in problems[0].message

    def test_bad_name(self, write_yaml: WriteYaml) -> None:
        """Job names must be 1-64 of ``A-Za-z0-9_-``."""
        problems = _errors(write_yaml("s.yaml", _job("a.b", "command: x")))
        assert len(problems) == 1
        assert "invalid name 'a.b'" in problems[0].message

    def test_missing_command(self, write_yaml: WriteYaml) -> None:
        """A job without a command is a problem."""
        problems = _errors(write_yaml("s.yaml", _job("j", "schedule: manual")))
        assert "command" in problems[0].message

    def test_unknown_top_level_key(self, write_yaml: WriteYaml) -> None:
        """Unknown top-level keys get a suggestion."""
        problems = _errors(write_yaml("s.yaml", "job: {}\n"))
        assert "did you mean 'jobs'" in problems[0].message

    def test_yaml_error(self, write_yaml: WriteYaml) -> None:
        """A YAML syntax error becomes a problem with its line."""
        problems = _errors(write_yaml("s.yaml", "a: 1\n\tb: 2\n"))
        assert problems[0].line == LINE_TWO

    def test_bad_values(self, write_yaml: WriteYaml) -> None:
        """Wrong types and out-of-range values are reported."""
        text = (
            "scheduler:\n  listen: localhost\n  timezone: Mars/Base\n  max_running: 0\n"
            "pools:\n  db: two\n"
            "calendars:\n  c:\n    weekdays: [funday]\n    holidays: [2026-13-01]\n"
            "    include: [missing]\n"
        )
        messages = [p.message for p in _errors(write_yaml("s.yaml", text))]
        expected = ["host:port", "unknown time zone", "at least 1", "integer", "weekday"]
        expected += ["invalid date", "unknown calendar 'missing'"]
        assert len(messages) == len(expected)
        for phrase, message in zip(expected, messages, strict=True):
            assert phrase in message


class TestPaths:
    """Paths in the scheduler section."""

    def test_relative_paths(self, write_yaml: WriteYaml) -> None:
        """Relative paths resolve next to the main file."""
        path = write_yaml("conf/s.yaml", "scheduler:\n  state_dir: state\n")
        settings = _ok(path).settings
        assert settings.state_dir == path.parent / "state"
        assert settings.log_dir == path.parent / "logs"

    def test_missing_main_file(self, tmp_path: Path) -> None:
        """A missing main file is a problem, not a crash."""
        problems = _errors(tmp_path / "nope.yaml")
        assert "cannot read" in problems[0].message
