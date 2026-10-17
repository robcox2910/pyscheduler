# PyScheduler Phase 1 (Core Engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working scheduler service that reads YAML config, validates it,
fires jobs on cron and calendar schedules across time zones, runs them
concurrently on PyNet's event loop through a minder process, honours
dependencies, retries, pools and operator actions, survives crashes, and
exposes a JSON API and a `pysched` CLI.

**Architecture:**
- Pure layers come first (YAML reader → config → calendars and schedules
  → forecast), then the runtime (minder, clock, launcher, store with group
  commit, engine), then the edges (HTTP API, CLI, service wiring).
- The engine keeps run state in memory as frozen dataclasses and writes
  every change to a journalled PyDB through a single group-commit writer
  before acting on it.
- All engine timing goes through a `Clock`, so tests use a fake wall clock.

**Tech Stack:**
- Python 3.14
- pynet v0.1.0 (`pynet.loop`, `pynet.process`)
- pyweb v0.2.0 (`App`, `AsyncServer`, `TestClient`)
- pydb v0.3.0 (`Database(journal=True)`, `Transaction`, DATE/TIMESTAMP)
- stdlib (`zoneinfo`, `argparse`, `urllib`)

**Spec:** `docs/superpowers/specs/2026-10-17-pyscheduler-design.md` (Phase 1 sections).

## Global Constraints

- Python ≥ 3.14; uv; ruff with the repo's rule set; pyright strict on `src` and `tests`; pytest coverage ≥ 80 % and `--timeout=10` per test.
- No `TYPE_CHECKING`, no `from __future__ import annotations`.
- Files stay under about 400 lines.
- Every public module, class and function has a docstring in imperative mood (Google style); test classes and methods too; named constants instead of magic numbers (PLR2004).
- Runtime dependencies are only pynet, pymq, pyweb and pydb, pinned by git tag in `[tool.uv.sources]`, plus the stdlib. **No PyYAML.**
- The pydb pin is `v0.3.0`. It is released before Task 11; until then the pin stays at v0.2.0.
- Package `pyscheduler`; CLI entry point `pysched = "pyscheduler.cli:main"`.
- POSIX only; CI on Linux.
- Names (jobs, calendars, pools): 1–64 chars of `[A-Za-z0-9_-]`.
- Run id format: `f"{job}.{run_date.isoformat()}.{seq}"`, with `seq` starting at 1 per `(job, run_date)`.
- Instants are aware UTC `datetime`s everywhere in the engine and store; local times only inside the schedule module.
- Defaults:
  - `max_running` 32
  - `listen` `127.0.0.1:8470`
  - `kill_grace` 10 s
  - `retention` 90 d
  - `memory_days` 7
  - retries `count: 0`, `delay: 1m`, `backoff: 2`, `max_delay: 10m`
  - `criticality: normal`
  - `overlap: queue`
  - `catch_up: latest`
  - `run_date: same_day`
  - `calendar: none`
  - timezone `UTC`
  - stdout `{log_dir}/{job}/{run_date}/{run_id}.{attempt}.out`, stderr the same with `.err`
- Every concept page goes in `docs/concepts/<name>.md`, kid-friendly with an everyday analogy and a runnable snippet, and is added to `mkdocs.yml` nav and `docs/index.md`. `mkdocs build --strict` must pass (Task 19 checks).
- Commits are conventional, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Scheduler restarted while a job's minder is still running.** The run
   must not start twice, and the attempt must finish from the minder's
   status file (Task 16 test `test_restart_adopts_live_minder`).
2. **A daily `30 1 * * *` job in Europe/London across 2026-10-25
   (fall-back) and 2027-03-28 (spring-forward).** It fires exactly once on
   each day: at 00:30 UTC on 10-25 (the first 01:30), and at 01:00 UTC on
   03-28 (the first instant after the gap) (Task 5).
3. **A command whose program does not exist.** The attempt ends
   `start_error`, retries follow the policy, the reason is visible, and the
   engine keeps running (Task 9 `test_missing_program`, Task 14
   `test_start_error_retries`).
4. **Killing a job whose shell started background grandchildren** (`sleep
   100 & sleep 100`). Every process in the group dies, and the run ends
   failed with reason `killed` (Task 10 `test_kill_stops_grandchildren`).
5. **A reload that removes a job that has waiting runs.** Those runs end
   `cancelled` (`removed by reload`) instead of waiting forever (Task 15
   `test_reload_removed_job_cancels_open_runs`).

---

## File map

```
src/pyscheduler/
  yamlish/__init__.py   parse(), YamlError, node types
  yamlish/nodes.py      Scalar, Seq, Map (with line numbers)
  yamlish/parser.py     the reader
  values.py             parse_duration, parse_time, parse_date, check_name
  templates.py          placeholders_in, render, expand_env
  calendars.py          BusinessCalendar, ALL_DAYS
  schedule/cron.py      CronExpr
  schedule/specs.py     CronSchedule, EverySchedule, AtSchedule, BusinessDaySchedule
  schedule/firings.py   Firing, resolve_local, firings_on, firings_between, next_firing
  schedule/forecast.py  ForecastEntry, forecast
  config/model.py       enums, Dependency, RetryPolicy, JobDef, Settings, Config, Problem, ConfigError
  config/fields.py      typed readers from yamlish nodes
  config/loader.py      load_config, build_config (files, includes, defaults)
  config/validate.py    validate_config (semantic checks and warnings)
  minder.py             python -m pyscheduler.minder
  engine/clock.py       Clock, Cancel, SystemClock, FakeClock
  engine/launcher.py    LaunchSpec, AttemptResult, AttemptHandle, Launcher, MinderLauncher, read_status
  engine/model.py       RunState, Trigger, Outcome, Run, Attempt, Event, make_run_id
  engine/runbook.py     RunBook (in-memory indexes)
  engine/deps.py        Verdict, DepResult, evaluate
  engine/admission.py   Admission, ReadyQueue
  engine/core.py        Engine (state, firings, transitions, commit)
  engine/attempts.py    attempt execution, timeouts, retries
  engine/actions.py     operator actions, backfill, reload
  engine/recovery.py    startup recovery, catch-up, retention
  store/schema.py       table definitions
  store/store.py        Store (sync PyDB access)
  store/writer.py       StoreWriter (group commit)
  api/auth.py           TokenTable, token middleware
  api/serialize.py      JSON shapes
  api/app.py            build_app
  client.py             ApiClient (urllib)
  service.py            serve()
  cli.py                pysched
  testing.py            FakeLauncher, FakeResult, helpers
```

Tests mirror the layout under `tests/`.

---

### Task 1: YAML-subset reader

**Files:**
- Create: `src/pyscheduler/yamlish/{__init__,nodes,parser}.py`
- Create: `docs/concepts/yaml.md`
- Test: `tests/yamlish/test_parser.py`

**Interfaces:**
- Produces:
  - `Scalar(value: str, line: int, quoted: bool)`, `Seq(items: list[Node], line: int)` and `Map(items: dict[str, Node], key_lines: dict[str, int], line: int)`: frozen dataclasses. `Node = Scalar | Seq | Map`.
  - `parse(text: str, filename: str = "<string>") -> Node`. An empty document gives `Map({}, {}, 1)`.
  - `YamlError(ValueError)` with attributes `filename`, `line`, `message`; `str()` is `"{filename}:{line}: {message}"`.

- [ ] **Step 1: Write failing tests**, table-driven, one test per behaviour:
  - `test_block_mapping_and_sequence`: `"a: 1\nb:\n  - x\n  - y\n"` → `Map` with `a=Scalar("1",1)` and `b=Seq([...])` (lines 3, 4).
  - `test_scalars_stay_strings`: `no`, `08:00`, `1e3`, `~` and `null` all come back as `Scalar` text, unchanged.
  - `test_flow_collections`: `"p: {a: 1, b: [x, 'y z']}"`.
  - `test_quoted_scalars`: double-quoted escapes `\n \t \" \\ é`, and single quotes with `''`.
  - `test_block_scalars`: `|`, `|-`, `|+`, `>` and `>-`, each with their exact expected strings.
  - `test_comments_and_doc_start`: a full-line comment, a trailing comment, `#` inside quotes, and a leading `---`.
  - `test_errors_have_lines`: each case raises `YamlError` with the given line:
    - a tab in the indent (line 2)
    - a duplicate key (line 3, message contains `duplicate key 'a'`)
    - an anchor `&x` (message contains `anchors`)
    - an alias `*x`, a tag `!!str`, a second document `---`
    - a complex key `? a`
    - bad indentation
    - an unterminated quote
    - an unterminated flow collection
  - `test_nested_sequence_of_mappings`: `"- a: 1\n  b: 2\n- c: 3\n"`.
- [ ] **Step 2: Run** `uv run pytest tests/yamlish -q`. Expect: FAIL (module missing).
- [ ] **Step 3: Implement.** Use a line-based recursive-descent reader driven by indentation, with a small tokenizer for flow collections and quoted scalars. Keep `parser.py` under 400 lines; split the flow and scalar helpers into `yamlish/_scalars.py` if it grows past that.
- [ ] **Step 4: Run** the tests. Expect: PASS. Run ruff and pyright: clean.
- [ ] **Step 5: Write `docs/concepts/yaml.md`** ("a recipe card with indents"; why we read everything as text first, the Norway problem). Create `mkdocs.yml` nav entries under a `Concepts` section.
- [ ] **Step 6: Commit** `feat: add YAML-subset reader with line numbers`.

### Task 2: Values, names and templates

**Files:**
- Create: `src/pyscheduler/values.py`, `src/pyscheduler/templates.py`
- Test: `tests/test_values.py`, `tests/test_templates.py`

**Interfaces:**
- Produces:
  - `parse_duration(text: str) -> timedelta`. It accepts `90s`, `15m`, `2h`, `1h30m`, `1d` and `1d2h3m4s`. It rejects an empty string, `0` without a unit, negatives and unknown units with `ValueError`.
  - `parse_time(text: str) -> time`: `HH:MM` with 00–23 and 00–59.
  - `parse_date(text: str) -> date`: ISO `YYYY-MM-DD`.
  - `check_name(text: str) -> None`: raises `ValueError` unless the text matches `^[A-Za-z0-9_-]{1,64}$`.
  - `PLACEHOLDERS = frozenset({"job", "run_date", "run_id", "attempt", "scheduled_at", "log_dir"})`
  - `placeholders_in(template: str) -> set[str]`: the names used in `{name}` or `{name:fmt}`. `{{` and `}}` are literal braces.
  - `render(template: str, values: Mapping[str, object]) -> str`: a `date`/`datetime` with a format spec uses `strftime`; without one it uses `isoformat()`. Raises `KeyError` for a missing name.
  - `expand_env(text: str, env: Mapping[str, str]) -> str`: expands `${NAME}` and leaves `$$` as `$`. Raises `KeyError(name)` when a variable is missing.

- [ ] **Step 1: Failing tests:**
  - `test_parse_duration_cases`: `"1h30m" == timedelta(minutes=90)`, `"1d" == timedelta(days=1)`; the bad inputs above raise.
  - `test_parse_time_bounds`: `"24:00"` and `"7:5"` raise; `"07:05"` is accepted.
  - `test_check_name`: `"load-warehouse_2"` is fine; `"a.b"`, `""`, a 65-character name and `"é"` raise.
  - `test_render_run_date_format`: `render("x_{run_date:%Y%m%d}", {"run_date": date(2026,10,17)}) == "x_20261017"`; `render("{run_date}", ...) == "x"`-style ISO output; `{{lit}}` stays `{lit}`.
  - `test_placeholders_in`: `"{job}/{run_date:%Y}/{{x}}"` gives `{"job","run_date"}`.
  - `test_expand_env`: `"${A}-$$"` with `A=1` gives `"1-$"`; a missing variable raises `KeyError`.
- [ ] **Step 2: Run** the tests. Expect: FAIL.
- [ ] **Step 3: Implement** with `string.Formatter().parse` for placeholders and a regex for durations.
- [ ] **Step 4: Run.** Expect: PASS. Lint clean.
- [ ] **Step 5: Commit** `feat: add duration, time, name and template helpers`.

### Task 3: Business calendars

**Files:**
- Create: `src/pyscheduler/calendars.py`, `docs/concepts/calendars-and-run-dates.md`
- Test: `tests/test_calendars.py`

**Interfaces:**
- Produces:
  - `BusinessCalendar(name: str, weekdays: frozenset[int], holidays: frozenset[date], extra_days: frozenset[date])`: frozen, with weekday 0 = Monday. Methods:
    - `is_business_day(d) -> bool`: an extra day is always a business day; otherwise a weekday that is not a holiday.
    - `previous(d) -> date`: the nearest business day strictly before `d`.
    - `next(d) -> date`: the nearest business day strictly after `d`.
    - `shift(d, n: int) -> date`: `n == 0` returns `d` unchanged; `n < 0` applies `previous` |n| times; `n > 0` applies `next` n times.
    - `nth_of_month(year, month, n) -> date | None`: n ≥ 1 counts from the start, n ≤ −1 from the end; `None` when the month has fewer business days.
  - `MAX_SEARCH_DAYS = 3660`. `previous` and `next` raise `ValueError` if they find nothing within that range.
  - `ALL_DAYS = BusinessCalendar("none", frozenset(range(7)), frozenset(), frozenset())`
  - `merge_calendars(name, base: BusinessCalendar, included: Sequence[BusinessCalendar]) -> BusinessCalendar`: the union of the holidays (used by calendar `include`).

- [ ] **Step 1: Failing tests:**
  - `test_uk_christmas_2026`: with weekdays Mon–Fri and holidays 2026-12-25, 2026-12-28 and 2027-01-01:
    - `previous(date(2026,12,29)) == date(2026,12,24)`
    - `next(date(2026,12,24)) == date(2026,12,29)`
  - `test_extra_day_counts`: a Saturday in `extra_days` is a business day.
  - `test_shift`: `shift(Mon 2026-10-19, -1) == Fri 2026-10-16`, and `shift(d, 0) == d` even on a Sunday.
  - `test_nth_of_month`: `nth_of_month(2026, 10, 1) == date(2026,10,1)`, `nth_of_month(2026,10,-1) == date(2026,10,30)`, and n = 25 gives `None`.
  - `test_empty_calendar_raises`: a calendar with no weekdays makes `previous` raise `ValueError`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `calendars-and-run-dates.md` (school days, and "which day's homework is this?"; `same_day` vs `previous_business_day` with the Monday-for-Friday example from the spec).
- [ ] **Step 6: Commit** `feat: add business calendars`.

### Task 4: Cron expressions

**Files:**
- Create: `src/pyscheduler/schedule/__init__.py`, `src/pyscheduler/schedule/cron.py`, `docs/concepts/cron.md`
- Test: `tests/schedule/test_cron.py`

**Interfaces:**
- Produces: `CronExpr.parse(text: str) -> CronExpr` (raises `ValueError` with a message naming the field), with:
  - `matches_day(d: date) -> bool`: applies the day-of-month, month and day-of-week rules, including the OR rule when both day fields are restricted. `L` in day-of-month; `mon#2` and `friL` in day-of-week; `7` also means Sunday.
  - `times() -> tuple[time, ...]`: every hour × minute combination, sorted.
  - `text: str`
  - Macros: `@hourly` = `0 * * * *`, `@daily` = `0 0 * * *`, `@weekly` = `0 0 * * 0`, `@monthly` = `0 0 1 * *`, `@yearly` = `0 0 1 1 *`.

- [ ] **Step 1: Failing tests:**
  - `test_fields`: `"*/15 9-17 * * mon-fri"` gives 36 times; it matches Mon 2026-10-19 and not Sat 2026-10-17.
  - `test_or_rule`: `"0 0 13 * fri"` matches Thu 2026-11-13 and Fri 2026-10-16.
  - `test_last_day`: `"0 0 L * *"` matches 2027-02-28 and not 2027-02-27.
  - `test_nth_weekday`: `"0 0 * * mon#2"` matches 2026-10-12 only in October 2026.
  - `test_last_weekday`: `"0 0 * * friL"` matches 2026-10-30.
  - `test_names_and_sunday7`: `jan`, `JAN`, `sun`, and `7` all parse as expected.
  - `test_bad_expressions`: each of these raises with the field named: `"60 * * * *"`, `"* * * *"`, `"*/0 * * * *"`, `"5-1 * * * *"`, `"0 0 L * mon#6"`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.** Parse each field into a frozenset; keep the "restricted" flags for the OR rule.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `cron.md` ("the alarm clock with many dials"; the OR rule with its example).
- [ ] **Step 6: Commit** `feat: add cron expressions with L, # and last-weekday`.

### Task 5: Schedules, firings, time zones and run dates

**Files:**
- Create: `src/pyscheduler/schedule/specs.py`, `src/pyscheduler/schedule/firings.py`, `docs/concepts/time-zones-and-dst.md`
- Test: `tests/schedule/test_specs.py`, `tests/schedule/test_firings.py`

**Interfaces:**
- Consumes: `CronExpr` (Task 4) and `BusinessCalendar` (Task 3).
- Produces, in `specs.py`:
  - `Schedule` protocol: `instants(self, day: date, tz: ZoneInfo, calendar: BusinessCalendar) -> list[datetime]`. It returns the UTC instants of firings whose landing day is `day` (local), before the calendar filter. For an `every` window that crosses midnight, every firing in it belongs to the day the window opened.
  - Frozen dataclasses implementing it:
    - `CronSchedule(cron)`
    - `EverySchedule(step: timedelta, start: time, end: time)`: the window includes its end
    - `AtSchedule(times: tuple[time,...])`
    - `BusinessDaySchedule(n: int, times: tuple[time,...])`
  - Every type has optional `days: frozenset[int] | None` and `months: frozenset[int] | None` filters on the landing day.
- Produces, in `firings.py`:
  - `Firing(scheduled_at: datetime, run_date: date)`: frozen, ordered.
  - `resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime` (UTC):
    - in a gap, the first instant after the gap
    - in a fold, the first occurrence (`fold=0`)
  - `class Timed(Protocol)` (what `JobDef` provides): `schedules: tuple[Schedule, ...]`, `timezone: ZoneInfo`, `run_date_rule: RunDateRule`.
    - `RunDateRule` is defined here as a `StrEnum` (`SAME_DAY = "same_day"`, `PREVIOUS_BUSINESS_DAY = "previous_business_day"`), and Task 6's model re-exports it.
  - `firings_on(job: Timed, day: date, calendar) -> list[Firing]`:
    - the union over the job's schedules, deduplicated by instant and sorted
    - dropped unless `calendar.is_business_day(day)`
    - `run_date` is `day` for `same_day`, or `calendar.previous(day)` for `previous_business_day`
  - `firings_between(job, calendar, start: datetime, end: datetime) -> Iterator[Firing]`: `start` is exclusive and `end` inclusive.
  - `next_firing(job, calendar, after: datetime) -> Firing | None`: searches up to `MAX_SEARCH_DAYS = 1464` days.

- [ ] **Step 1: Failing tests, golden cases** (use a tiny dataclass that satisfies `Timed` in the tests):
  - `test_london_fall_back_once`: `30 1 * * *` in Europe/London on 2026-10-25 gives exactly `[2026-10-25T00:30Z]`.
  - `test_london_spring_gap`: the same job on 2027-03-28 gives `[2027-03-28T01:00Z]`.
  - `test_new_york_dst`: `30 2 * * *` America/New_York on 2027-03-14 gives `[07:00Z]`; on 2026-11-01, `01:30` gives `[05:30Z]` only.
  - `test_every_elapsed_across_fall_back`: `every 15m between 00:00-03:00` in London on 2026-10-25 gives 17 firings 15 minutes apart in UTC, from 2026-10-24T23:00Z to 2026-10-25T03:00Z: 13 wall-clock slots plus the 4 from the repeated hour.
  - `test_every_window_crosses_midnight`: `22:00-02:00` every 1h, UTC, on day D gives 22, 23, 00, 01 and 02. Those firings belong to D, and D+1's own window starts at D+1 22:00.
  - `test_previous_business_day_monday_for_friday`: `0 2 * * *` with uk-business and `previous_business_day` from Mon 2026-10-19 to Sun 2026-10-25 gives these run dates:
    - Mon → Fri 2026-10-16
    - Tue → Mon 10-19, Wed → Tue, Thu → Wed, Fri → Thu
    - no firing on Saturday or Sunday
  - `test_business_day_schedule`: `BusinessDaySchedule(-1, (time(18),))` fires only on 2026-10-30 in October.
  - `test_union_dedupes`: `0 9 * * *` plus `at 09:00` gives one firing.
  - `test_property_next_equals_between` (seeds 1–20): 200 chained `next_firing` calls equal `firings_between` over the same span, strictly increasing. Use random cron and every schedules and random time zones from a fixed list.
  - `test_never_fires`: `0 0 30 2 *` gives `next_firing(...) is None`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.** Go day by day. `firings_between` iterates local days from `start`'s local date minus 1, to catch windows opened the day before.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `time-zones-and-dst.md` ("the clocks change, and the alarm still rings once").
- [ ] **Step 6: Commit** `feat: add schedules and firings with DST-safe time zones`.

### Task 6: Config model and loader

**Files:**
- Create: `src/pyscheduler/config/{__init__,model,fields,loader}.py`
- Test: `tests/config/test_loader.py`, `tests/config/conftest.py` (a helper that writes YAML files to `tmp_path`)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces, in `model.py`:
  - `Criticality(IntEnum)`: `LOW=0`, `NORMAL=1`, `HIGH=2`, `CRITICAL=3`, with `from_text`.
  - `DepOn(StrEnum)`: `success`, `failure`, `done`.
  - `Overlap(StrEnum)`: `queue`, `skip`, `allow`.
  - `CatchUp(StrEnum)`: `latest`, `all`, `none`.
  - `RunDateRule`, re-exported from Task 5.
  - `SourceRef(file: str, line: int)`
  - `Dependency(job: str, on: DepOn = DepOn.SUCCESS, run_date_offset: int = 0, source: SourceRef)`
  - `RetryPolicy(count=0, delay=1m, backoff=2.0, max_delay=10m)` with `delay_for(n: int) -> timedelta`. For n ≥ 1 it returns `min(delay * backoff**(n-1), max_delay)`.
  - `MustStartBy = time | timedelta`
  - `JobDef`, frozen, with fields:
    - `name: str`
    - `command: str | tuple[str, ...]`
    - `cwd: str | None`, `env: Mapping[str, str]`
    - `schedules: tuple[Schedule, ...]`, `manual: bool`
    - `calendar: str | None` (None = `none`), `timezone: ZoneInfo`, `run_date_rule: RunDateRule`
    - `depends_on: tuple[Dependency, ...]`
    - `retries: RetryPolicy`, `timeout: timedelta | None`
    - `criticality: Criticality`
    - `pools: Mapping[str, int]`
    - `overlap: Overlap`, `catch_up: CatchUp`
    - `stdout: str`, `stderr: str` (the literal `"stdout"` means merged)
    - `min_runtime: timedelta | None`, `max_runtime: timedelta | None`, `must_start_by: MustStartBy | None`
    - `alerts: Mapping[str, object]` (stored as given; phase 2 checks it)
    - `agent: str | None`, `agent_tags: tuple[str, ...]`
    - `tags: tuple[str, ...]`, `description: str`, `owner: str`
    - `enabled: bool`
    - `source: SourceRef`

    It has the property `dependency_only` (no schedules, not manual, has `depends_on`).
  - `Settings`, frozen: `name`, `timezone: ZoneInfo`, `state_dir: Path`, `log_dir: Path`, `max_running: int`, `listen: tuple[str, int]`, `api_tokens_file: Path | None`, `kill_grace: timedelta`, `retention: timedelta`, `memory_days: int`. Paths are resolved relative to the main config file.
  - `Problem(file: str, line: int, message: str, job: str | None = None, severity: Literal["error","warning"] = "error")`. Its `__str__` is `f"{file}:{line}: job {job}: {message}"`, without the `job …:` part when `job` is None.
  - `ConfigError(Exception)` with `problems: tuple[Problem, ...]`; its message lists them all.
  - `Config`, frozen: `settings`, `calendars: Mapping[str, BusinessCalendar]`, `pools: Mapping[str, int]`, `jobs: Mapping[str, JobDef]`, `warnings: tuple[Problem, ...]`, `digest: str` (sha256 of the concatenated sources, in file order), `sources: Mapping[str, str]` (path → text). Methods:
    - `calendar_for(job: JobDef) -> BusinessCalendar` (`ALL_DAYS` when the calendar is None)
    - `dependents(job: str) -> frozenset[str]`: the reverse dependency index, precomputed
- Produces, in `loader.py`:
  - `build_config(path: Path, *, env: Mapping[str, str] | None = None) -> tuple[Config, list[Problem]]`: field-level problems only. When there are errors, the returned `Config` is partial and must not be used.
  - `load_config(path, *, env=None) -> Config`: raises `ConfigError` if there is any error. Task 7 adds the semantic checks to it.
- `fields.py` holds the typed readers (`read_str`, `read_int`, `read_bool` accepting only `true`/`false`, `read_duration`, `read_list`, `read_schedule`, …). Each appends a `Problem` and returns a default instead of raising, so **all** problems are collected.

- [ ] **Step 1: Failing tests:**
  - `test_full_example_loads`: the spec's example config (the "Configuration files" block) loads. Check:
    - `jobs["load-warehouse"].criticality is Criticality.CRITICAL`
    - `retries.count == 2`, from defaults
    - `pools["database"] == 2`
    - the `extract-trades` command is a str, and the `load-warehouse` command is a tuple
  - `test_defaults_merge_job_wins`: a job's `retries: {count: 5}` merges over defaults key by key, giving `count 5` and `delay 1m`.
  - `test_include_globs`: `include: ["jobs/*.yaml"]` loads two files. A duplicate job name across them is a `Problem` naming both files and lines.
  - `test_collects_all_problems`: one file with three errors gives three Problems with the right lines:
    - an unknown key `comand` (message contains `did you mean 'command'`)
    - `retries: {count: lots}`
    - `timeout: 5x`
  - `test_bool_is_strict`: `enabled: no` is a problem; it must be `false`.
  - `test_env_expansion`: `${HOME_DIR}` expands; a missing variable is a Problem with its line.
  - `test_bad_name`: the job name `a.b` gives a Problem.
  - `test_schedule_forms`, one per form:
    - a cron string
    - a list of schedules
    - `{every: 15m, between: "08:00-18:00"}`
    - `{at: ["09:00","17:30"]}`
    - `{business_day: -1, at: "18:00", months: [3,6,9,12]}`
    - `manual`
  - `test_dependency_forms`: the four forms from the spec's "Dependencies" block.
  - `test_relative_paths`: `state_dir: state` resolves next to the main file.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.** Use `difflib.get_close_matches` for "did you mean".
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Commit** `feat: add config model and loader with collected problems`.

### Task 7: Semantic validation

**Files:**
- Create: `src/pyscheduler/config/validate.py`, `docs/concepts/validation.md`
- Modify: `src/pyscheduler/config/loader.py` (`load_config` calls `validate_config`)
- Test: `tests/config/test_validate.py`

**Interfaces:**
- Produces: `validate_config(config: Config, *, today: date | None = None) -> list[Problem]`, with errors and warnings. `today` defaults to today in UTC and anchors the 4-year and 366-day horizons. `load_config` stores the warnings on the `Config` and raises on errors.

- [ ] **Step 1: Failing tests**, one per rule in the spec's "Validation" section. Each asserts the Problem's job, its line, and a key phrase:
  - errors:
    - `unknown job`
    - `cycle` (the message lists the cycle path, e.g. `a -> b -> a`)
    - `positive run-date offset`
    - `different calendars` (a uk-business job depending on a us-business job)
    - `calendar job depends on a job with no calendar`
    - `business_day needs a calendar`
    - `never fires` (`0 0 30 2 *`)
    - `unknown time zone`
    - `unknown calendar`
    - `unknown pool`
    - `needs 3 slots but pool 'database' has 2`
    - `calendar include cycle`
    - `unknown placeholder {rundate}` (in command, cwd, env, stdout and stderr)
    - `min_runtime must be less than max_runtime`
    - `max_runtime exceeds timeout`
    - `negative retry count`
    - `backoff must be at least 1`
    - `agents arrive in phase 4`
    - `no schedule, no dependencies and not manual`
  - warnings:
    - `would wait forever`: `downstream` on Mon–Sun daily, `upstream` Mon–Fri only, same calendar `none`
    - `fires more than once per run date`
    - `overlap allow with pools`
  - `test_offset_cycle_allowed`: a depends on b with `run_date: -1`, and b depends on a with offset 0, gives no cycle error.
  - `test_all_reported_together`: 4 different errors in one config give a `ConfigError` with 4 problems.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.** Cycle detection is a DFS over offset-0 edges. "Would wait forever" compares `firings_on` landing run dates over 366 days: a downstream run date where an upstream that is not dependency-only has no firing, and has no path to get one.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `validation.md` ("the teacher who checks your work before the test").
- [ ] **Step 6: Commit** `feat: add semantic config validation`.

### Task 8: Forecast

**Files:**
- Create: `src/pyscheduler/schedule/forecast.py`, `docs/concepts/forecast.md`
- Test: `tests/schedule/test_forecast.py`

**Interfaces:**
- Produces:
  - `ForecastEntry(job: str, run_date: date, expected_start: datetime, expected_duration: timedelta | None, via: str | None)`: frozen. `via` is the upstream job for dependency-only entries.
  - `forecast(config: Config, start: datetime, end: datetime, *, durations: Mapping[str, Sequence[timedelta]] | None = None) -> list[ForecastEntry]`, sorted by `(expected_start, job)`.
  - Expected duration is the median of up to the last 10 durations given. A dependency-only job's expected start is the max of its upstreams' `expected_start + duration`, or plain `expected_start` when the duration is unknown. Disabled and manual jobs are left out.

- [ ] **Step 1: Failing tests:**
  - `test_chain_expected_start`: `a` fires at 18:30 with durations [10m, 20m, 30m] (median 20m), and `b` depends on `a`. Then `b.expected_start == 18:50` and `b.via == "a"`.
  - `test_unknown_duration`: with no durations, `b` starts at `a`'s start.
  - `test_window_bounds`: firings outside `(start, end]` are excluded.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `forecast.md` (the weather forecast for jobs).
- [ ] **Step 6: Commit** `feat: add schedule forecast`.

### Task 9: The minder

**Files:**
- Create: `src/pyscheduler/minder.py`, `docs/concepts/the-minder.md`
- Test: `tests/test_minder.py` (real subprocesses through `subprocess.run([sys.executable, "-m", "pyscheduler.minder", ...])`)

**Interfaces:**
- Produces:
  - The CLI `python -m pyscheduler.minder --status PATH --stdout PATH [--stderr PATH] -- ARGV...`. Without `--stderr`, stderr goes into the stdout file.
  - Behaviour:
    - creates the parent directories and opens the logs in append-binary mode
    - writes `<status>.pid` containing JSON `{"pid": <minder pid>, "started_at": iso}`
    - runs ARGV with `stdin=DEVNULL`
    - on SIGTERM or SIGINT, forwards the signal to the child and keeps waiting for it
    - writes the status JSON atomically (temp file in the same directory, fsync, rename): `{"exit_code": int|None, "signal": int|None, "started_at": iso, "ended_at": iso, "start_error": str|None}`
    - exits with the child's code, `128 + signal`, or `127` on a start error
  - Module functions for the launcher: `write_status(path: Path, status: Mapping[str, object]) -> None` and `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Failing tests:**
  - `test_exit_code_and_logs`: `sh -c 'echo out; echo err >&2; exit 3'` gives status `exit_code 3`, the logs contain `out` and `err` (separate files), and the minder exits 3.
  - `test_merged_stderr`: without `--stderr`, both lines are in the one file.
  - `test_appends`: running twice appends to the logs.
  - `test_missing_program` (Review Focus 3): `["no-such-prog-xyz"]` gives a `start_error` containing `No such file`, `exit_code None`, and the minder exits 127.
  - `test_sigterm_forwarded`: start `sh -c 'trap "exit 7" TERM; sleep 5 & wait'`, wait for the `.pid` file, send SIGTERM to the minder, and expect status `exit_code 7`.
  - `test_signal_death`: the child kills itself with SIGKILL, giving `signal 9`, `exit_code None`, and the minder exits 137.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement** with stdlib `subprocess` and `signal`. This is a plain blocking program, not an event loop.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `the-minder.md` ("a babysitter who writes a note when the job's done").
- [ ] **Step 6: Commit** `feat: add the minder job wrapper`.

### Task 10: Clock and launcher

**Files:**
- Create: `src/pyscheduler/engine/__init__.py`, `src/pyscheduler/engine/clock.py`, `src/pyscheduler/engine/launcher.py`, `src/pyscheduler/testing.py`
- Test: `tests/engine/test_clock.py`, `tests/engine/test_launcher.py`, `tests/test_testing.py`

**Interfaces:**
- Produces, in `clock.py`:
  - `Cancel` protocol (`cancel() -> None`)
  - `Clock` protocol:
    - `now() -> datetime`, aware UTC
    - `call_at(when: datetime, callback: Callable[[], None]) -> Cancel`
    - `async sleep_until(when: datetime) -> None`
  - `SystemClock()`: uses the running pynet loop. `call_at` arms `loop.call_later(min(remaining, MAX_NAP))` with `MAX_NAP = 60.0`, and re-arms until `now() >= when`.
  - `FakeClock(start: datetime)`:
    - `now()`, `call_at`, `sleep_until`
    - `async advance(by: timedelta) -> None` and `async advance_to(when: datetime) -> None`: these fire due callbacks in time order (ties in arrival order), setting `now` to each callback's time, and `await pynet.loop.sleep(0)` a few times (`SETTLE_TURNS = 5`) after each one so woken tasks run.
- Produces, in `launcher.py`:
  - `LaunchSpec(run_id: str, attempt: int, argv: tuple[str, ...], cwd: str | None, env: Mapping[str, str], stdout_path: Path, stderr_path: Path | None, status_path: Path)`
  - `AttemptResult(exit_code: int | None, signal: int | None, started_at: datetime | None, ended_at: datetime, start_error: str | None)` with the property `succeeded` (exit 0, no signal, no start error).
  - `AttemptHandle` protocol: `pid: int | None`, `async wait() -> AttemptResult`, `terminate() -> None`, `kill() -> None`. The last two signal the whole process group.
  - `Launcher` protocol: `async launch(spec: LaunchSpec) -> AttemptHandle`
  - `MinderLauncher(python: str = sys.executable)`: `create_process([python, "-m", "pyscheduler.minder", ...], cwd=spec.cwd, env=spec.env, stdout=False, stderr=False, new_session=True)`. `wait()` awaits the process, then `read_status`. When the status is missing, it synthesises a result from the minder's own return code (`signal = -code` when negative).
  - `read_status(path: Path) -> AttemptResult | None`
- Produces, in `testing.py`:
  - `FakeResult(duration: timedelta = 1m, exit_code: int | None = 0, signal: int | None = None, start_error: str | None = None, ignores_term: bool = False)`
  - `FakeLauncher(clock: FakeClock)`:
    - `plan(job: str, *results: FakeResult) -> None`: the job's results, used in order, with the last one repeating
    - `launched: list[LaunchSpec]`
    - `running: dict[str, FakeHandle]` (by run id)
    - Each handle finishes when the fake clock reaches `start + duration`. `terminate()` ends it at once with signal 15, unless `ignores_term`; `kill()` always ends it with signal 9. A `start_error` result fails at launch time with `start_error` set.

- [ ] **Step 1: Failing tests:**
  - `test_fake_clock_orders_callbacks`: callbacks at t+2 and t+1 fire in time order, and `now` equals each callback's time when it fires.
  - `test_fake_clock_sleep_until_wakes_task`: a task sleeping until t+5 wakes after `advance(5s)`, not after 4s.
  - `test_system_clock_call_at_short`: a real-time 0.05 s callback fires inside `pynet.loop.run`.
  - `test_minder_launcher_runs_command` (real): `sh -c 'exit 4'` gives `exit_code 4`, and the log files exist.
  - `test_kill_stops_grandchildren` (Review Focus 4): `sh -c 'sleep 100 & sleep 100 & wait'` through the `MinderLauncher`, then `kill()`. `wait()` returns promptly, and `os.killpg(pgid, 0)` raises `ProcessLookupError` afterwards.
  - `test_fake_launcher_plan`: plan `[FakeResult(exit_code=1), FakeResult()]`; the first launch fails at +1m and the second succeeds.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Commit** `feat: add clock abstraction and minder launcher`.

### Task 11: Store and group-commit writer

**Pre-step (controller):** pydb v0.3.0 has been released. Bump `pydb` in `[tool.uv.sources]` to `tag = "v0.3.0"`, run `uv lock --upgrade-package pydb`, and commit `chore: pin pydb v0.3.0`.

**Files:**
- Create: `src/pyscheduler/engine/model.py`, `src/pyscheduler/store/{__init__,schema,store,writer}.py`, `docs/concepts/group-commit.md`, `docs/concepts/runs-and-states.md`
- Test: `tests/engine/test_model.py`, `tests/store/test_store.py`, `tests/store/test_writer.py`

**Interfaces:**
- Produces, in `engine/model.py`:
  - `RunState(StrEnum)`: `scheduled`, `waiting`, `blocked`, `queued`, `running`, `retry_wait`, `held`, `succeeded`, `failed`, `skipped`, `cancelled`.
  - `FINAL_STATES = frozenset({SUCCEEDED, FAILED, SKIPPED, CANCELLED})`
  - `Trigger(StrEnum)`: `schedule`, `dependency`, `manual`, `backfill`, `catch_up`.
  - `Outcome(StrEnum)`: `succeeded`, `failed`, `timeout`, `killed`, `lost`, `start_error`.
  - `Run`, frozen (changed with `dataclasses.replace`): `run_id`, `job`, `run_date: date`, `scheduled_at: datetime`, `state: RunState`, `reason: str`, `attempt: int` (0 before the first attempt), `rerun: int`, `trigger: Trigger`, `config_version: int`, `created_at: datetime`, `updated_at: datetime`, `held_from: RunState | None`.
  - `Attempt`, frozen: `run_id`, `attempt: int`, `started_at: datetime`, `ended_at: datetime | None`, `exit_code: int | None`, `signal: int | None`, `outcome: Outcome | None`, `pid: int | None`, `stdout_path: str`, `stderr_path: str`, `status_path: str`, `reason: str`.
  - `Event`, frozen: `at: datetime`, `actor: str`, `run_id: str | None`, `action: str`, `detail: str` (JSON text).
  - `make_run_id(job: str, run_date: date, seq: int) -> str` and `parse_run_id(run_id: str) -> tuple[str, date, int]`.
- Produces, in `store/store.py`, `Store`. Its methods are synchronous and thread-safe through PyDB's lock:
  - `Store.open(state_dir: Path, *, sync: bool = True) -> Store`: creates `state_dir`, opens `Database(state_dir / "db", journal=True, sync=sync)`, and creates the tables and indexes if they are missing.
  - `apply(batch: Batch) -> None`: one `Transaction`; upserts runs by `run_id` and attempts by `(run_id, attempt)`, appends events, upserts engine keys, inserts configs, and deletes `deleted_run_ids` along with their attempts and events.
  - `load_open_runs() -> list[Run]`
  - `load_final_runs_since(run_date: date) -> list[Run]`
  - `get_run(run_id) -> Run | None`
  - `query_runs(*, run_date: date | None = None, job: str | None = None, state: RunState | None = None, limit: int = 500) -> list[Run]`, newest `scheduled_at` first
  - `attempts_for(run_id) -> list[Attempt]` and `events_for(run_id) -> list[Event]`
  - `recent_durations(job: str, limit: int = 10) -> list[timedelta]`: from succeeded attempts, newest first
  - `get_engine(key) -> str | None`
  - `latest_config() -> ConfigRecord | None`
  - `final_run_ids_before(run_date: date, limit: int) -> list[str]`
  - `close() -> None`: checkpoints, then closes
- Also produced:
  - `Batch(runs=[], attempts=[], events=[], engine={}, configs=[], deleted_run_ids=[])`: a mutable dataclass with `merge(other)` and `is_empty`.
  - `ConfigRecord(version: int, loaded_at: datetime, digest: str, sources_json: str)`
- Produces, in `store/writer.py`, `StoreWriter(store: Store)`:
  - `async start()`
  - `async save(batch: Batch) -> None`: returns after the batch is durable, and re-raises the commit error to every waiter in a failed group
  - `async close()`: flushes pending batches, then stops
  - `commits: int`: the number of `apply` calls made
  - Algorithm: one writer task waits on a pynet `Event`. It swaps out the pending list, merges the batches in order (a later upsert of the same key wins), calls `await run_in_thread(store.apply, merged)`, then resolves all the waiters' futures.

- [ ] **Step 1: Failing tests:**
  - `test_run_id_roundtrip`: `make_run_id("a-b", date(2026,10,17), 3) == "a-b.2026-10-17.3"` and `parse_run_id` inverts it.
  - `test_apply_upsert_and_reload`: apply a run twice with different states, close, reopen, and `get_run` shows the second state. DATE and TIMESTAMP values round-trip, and the UTC tzinfo is kept.
  - `test_open_runs_only_non_final`.
  - `test_delete_cascades`: deleting a run id removes its attempts and events.
  - `test_recent_durations_newest_first`.
  - `test_group_commit_batches`: spawn 100 concurrent `save()` calls inside `pynet.loop.run` against a store whose `apply` sleeps 20 ms in its thread (a wrapper). Assert `writer.commits < 10`, and that all 100 runs are durable after reopening.
  - `test_failed_commit_raises_to_all_waiters`: an `apply` that raises gives every waiter of that group the error; the next save works.
  - `test_save_after_close_raises`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.** Use PRIMARY KEY `run_id` on `runs`, plus indexes on `runs.job`, `runs.run_date`, `runs.state` and `attempts.run_id`. Lookups must use indexes, never a full scan per upsert.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `group-commit.md` ("one trip to the post box for many letters") and `runs-and-states.md` (the traffic lights of a run, with the state diagram from the spec).
- [ ] **Step 6: Commit** `feat: add journalled store with group commit`.

### Task 12: RunBook and dependency evaluation

**Files:**
- Create: `src/pyscheduler/engine/runbook.py`, `src/pyscheduler/engine/deps.py`, `docs/concepts/dependencies.md`
- Test: `tests/engine/test_runbook.py`, `tests/engine/test_deps.py`

**Interfaces:**
- Produces, in `runbook.py`, `RunBook()`:
  - `put(run: Run) -> None`: adds or replaces the run and keeps every index in step
  - `get(run_id) -> Run | None`
  - `runs_for(job, run_date) -> list[Run]`, sorted by `(scheduled_at, run_id)`
  - `latest_for(job, run_date) -> Run | None`
  - `by_scheduled(job, scheduled_at) -> Run | None`, for scheduled-trigger uniqueness
  - `dependency_run(job, run_date) -> Run | None`, for dependency-only uniqueness
  - `active_for(job) -> list[Run]`: runs that are QUEUED or RUNNING
  - `overlap_waiting(job, run_date) -> Run | None`: the WAITING run whose reason starts with `OVERLAP_REASON = "waiting for previous run"`
  - `next_seq(job, run_date) -> int`
  - `open_runs() -> list[Run]`
  - `evict_final_before(run_date: date) -> int`
  - Published views: `publish(run: Run)`, `published(run_id) -> Run | None`, `published_runs(*, run_date=None, job=None, state=None) -> list[Run]`. These hold the last *durable* copy, and the API reads only them.
- Produces, in `deps.py`:
  - `Verdict(StrEnum)`: `ready`, `wait`, `block`, `skip`
  - `DepResult(verdict, reason: str)`
  - `evaluate(job: JobDef, run_date: date, calendar: BusinessCalendar, book: RunBook) -> DepResult`
  - Rules, from the spec, decided per dependency on the upstream's `latest_for(dep.job, calendar.shift(run_date, dep.run_date_offset))`:
    - none, or not final → wait, with reason `f"waiting for {job} ({date}): {state or 'no run yet'}"`
    - `success`: SUCCEEDED or SKIPPED → ok; FAILED or CANCELLED → block (`f"blocked: {job} ({date}) {state}"`)
    - `failure`: FAILED → ok; SUCCEEDED, SKIPPED or CANCELLED → skip (`f"skipped: {job} ({date}) did not fail"`)
    - `done`: any final state → ok
    - Combine with precedence skip > block > wait > ready; the reason comes from the first dependency (in config order) that has the winning verdict.

- [ ] **Step 1: Failing tests:**
  - `test_put_replaces_and_reindexes`: a state change moves the run between `open_runs` and final.
  - `test_latest_for_picks_latest_scheduled`.
  - `test_published_separate_from_current`: after `put` without `publish`, `published(id)` is still the old copy.
  - `test_evict`.
  - A deps verdict table: one test per row of the rules above, including the `run_date: -1` lookup over a weekend (with the calendar from Task 3) and the precedence cases.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `dependencies.md` ("you can't ice the cake before it's baked"; on success, failure and done; blocked vs skipped).
- [ ] **Step 6: Commit** `feat: add run book and dependency evaluation`.

### Task 13: Admission control

**Files:**
- Create: `src/pyscheduler/engine/admission.py`, `docs/concepts/pools-and-limits.md`
- Test: `tests/engine/test_admission.py`

**Interfaces:**
- Produces, `Admission(max_running: int, pools: Mapping[str, int])`:
  - `fits(needs: Mapping[str, int]) -> bool`
  - `acquire(needs) -> None` (raises if it doesn't fit)
  - `release(needs) -> None`
  - `running: int`
  - `in_use(pool) -> int`
  - `set_limits(max_running, pools) -> None`: for reload; may leave usage over the new limits until runs finish
- Produces, `ReadyQueue()`:
  - `push(run: Run, criticality: Criticality, needs: Mapping[str, int]) -> None`
  - `remove(run_id) -> bool`
  - `take_startable(admission: Admission, *, force: frozenset[str] = frozenset()) -> list[Run]`: acquires slots as it goes. The order is `(-criticality, scheduled_at, run_id)`. An entry that doesn't fit is skipped, and later entries can still start.
  - `__len__`, `__contains__(run_id)`

- [ ] **Step 1: Failing tests:**
  - `test_priority_order`: a critical run scheduled later starts before a normal one.
  - `test_busy_pool_does_not_block_others`: pool `db` is full; a queued db run stays queued while a non-db run behind it starts.
  - `test_global_cap`: with `max_running=2`, 3 runs queued give 2 started.
  - `test_release_then_take`.
  - `test_set_limits_shrink`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement** with `heapq` plus lazy deletion.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `pools-and-limits.md` ("only two kids on the trampoline").
- [ ] **Step 6: Commit** `feat: add admission control with pools`.

### Task 14: Engine core: firings, transitions, attempts and retries

**Files:**
- Create: `src/pyscheduler/engine/core.py`, `src/pyscheduler/engine/attempts.py`, `docs/concepts/retries-and-backoff.md`, `docs/concepts/scheduler.md`
- Test: `tests/engine/test_engine_core.py`, `tests/engine/test_attempts.py`, `tests/engine/conftest.py`

The conftest provides a fixture `harness(tmp_path)`. It builds a `Config` from a YAML string, a `Store.open(tmp_path, sync=False)`, a `FakeClock(2026-10-19T00:00Z)`, a `FakeLauncher` and an `Engine`. It also provides `async run_engine(test_coro)`, which runs everything inside `pynet.loop.run`.

**Interfaces:**
- Produces, `Engine(config: Config, store: Store, *, clock: Clock, launcher: Launcher, config_version: int = 1)`:
  - `async start() -> None`. In this task it starts the writer and the firing task; Task 16 adds recovery and catch-up before them.
  - `async stop(*, kill_running: bool = False) -> None`: stops creating and starting runs, cancels timers, flushes the writer and closes the store. Running minders are left alone unless `kill_running`.
  - `config: Config`, `book: RunBook`, `admission: Admission`, `writer: StoreWriter`
  - `subscribe(callback: Callable[[Run], None]) -> Callable[[], None]`: called with each run after its change is durable (after `publish`)
  - `async settle() -> None`: test helper; waits until the writer is idle and no transition work is pending
  - Internal contract, used by Tasks 15 and 16:
    - `async _transition(changes: list[Run], *, attempts=(), events=()) -> None`: puts the runs in the book, awaits `writer.save`, publishes, then notifies the subscribers
    - `_create_run(job, run_date, scheduled_at, trigger) -> Run | None`: enforces uniqueness and overlap rules, and creates dependency-only downstream runs (transitively)
    - `_reevaluate(job_names: Iterable[str]) -> None`: re-checks the open runs of those jobs and their dependents, and moves them between waiting, blocked, skipped and queued
    - `_pump() -> None`: starts the queued runs that fit
- Rules:
  - **Firing task:** loops `next_firing` across jobs through a heap keyed by `scheduled_at`. It sleeps with `clock.sleep_until`, creates runs (`Trigger.SCHEDULE`), and saves the engine key `last_tick` = that firing's instant.
  - **Overlap:**
    - `queue`: if the job has an active run, the new run waits with reason `OVERLAP_REASON`. An older overlap-waiting run of the **same run date** is cancelled with reason `superseded`.
    - `skip`: the new run is created as `cancelled`, reason `skipped: overlap`.
    - `allow`: no check.
    - When an active run ends, the oldest overlap-waiting run of that job (by `run_date`, then `scheduled_at`) is re-evaluated.
  - **Attempts** (in `attempts.py`):
    - On start: acquire the slots, `attempt += 1`, and render the command and log paths with the template values `job`, `run_date`, `run_id`, `attempt`, `scheduled_at` and `log_dir`. A str command becomes `("/bin/sh", "-c", text)`. The env is `os.environ` + the job env + `PYSCHED_JOB`, `PYSCHED_RUN_DATE`, `PYSCHED_RUN_ID` and `PYSCHED_ATTEMPT`. The status path is `{state_dir}/status/{run_id}.{attempt}.json`.
    - Commit `running` together with the Attempt row (no pid yet), then launch, then commit the pid.
    - Arm the timeout with `clock.call_at(start + timeout)` → `terminate()`, and `call_at(+kill_grace)` → `kill()`.
    - Outcome mapping: exit 0 → SUCCEEDED; `start_error` → START_ERROR; killed by our timeout → TIMEOUT; killed by an operator (Task 15 sets a flag) → KILLED; anything else → FAILED.
    - The `rerun` column holds the attempt count when the latest rerun began (0 initially), so `tries = attempt - rerun` counts the attempts since then.
    - Retry when the outcome is not SUCCEEDED or KILLED and `tries <= retries.count`. The run goes to `retry_wait` with `clock.call_at(now + retries.delay_for(tries))` → queued.
    - Otherwise the run ends `failed`, with a reason such as `exit 3`, `timeout`, `start error: No such file …`, or `lost`.
    - Release the slots and `_pump()`.

- [ ] **Step 1: Failing tests** (fake clock and fake launcher):
  - `test_scheduled_chain_runs_in_order`: `a` at `30 18 * * *`, `b` depends on `a`. `advance_to(18:30)`: `a` launches; +1m: `a` succeeded, `b` launched; +1m: `b` succeeded. The run dates are 2026-10-19, and the triggers are schedule and dependency.
  - `test_blocked_then_not_failed`: `a` fails with no retries → `b` is `blocked` with reason containing `blocked: a`.
  - `test_on_failure_skipped`: cleanup with `on: failure`; `a` succeeds → cleanup is `skipped`.
  - `test_retry_backoff_delays`: retries `{count: 2, delay: 1m, backoff: 2}`. Fail, fail, succeed: the attempts start at 0, +1m (end) + 1m, then (end) + 2m. Final state succeeded, attempt 3.
  - `test_start_error_retries` (Review Focus 3): `FakeResult(start_error="No such file")` twice with count 1 gives final `failed`, with the reason containing `No such file`.
  - `test_timeout_terminates_then_kills`: timeout 5m, kill_grace 10s, a result that `ignores_term`. At +5m `terminate`, at +5m10s `kill`; outcome `timeout`.
  - `test_overlap_queue_supersedes_same_run_date`: an `every 15m` job whose result takes 50m:
    - at +15m a waiting run exists
    - at +30m the +15m run is `cancelled` (`superseded`) and the +30m run waits
    - after the first run ends, the +30m run starts
  - `test_overlap_other_run_date_never_superseded`.
  - `test_overlap_skip`.
  - `test_pool_limits_concurrency`: 5 jobs needing `db: 1` with pool `db: 2` → never more than 2 running at once (check `launcher.running` at each step).
  - `test_write_ahead`: a subscriber records each run's state, and a store spy checks that each `running` state was durable before `launcher.launch` was called (wrap `Store.apply` and `FakeLauncher.launch` with an ordering log).
  - `test_published_view_lags_until_durable`: with `apply` patched to block on a `threading.Event`, `book.published(id)` shows the old state until the event is set.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.** Keep `core.py` and `attempts.py` each under 400 lines. The attempt coroutine is spawned with `pynet.loop.spawn` and tracked in a set so that `stop()` can wait for it or cancel it.
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `retries-and-backoff.md`, and `scheduler.md` (the school timetable; an overview of the whole engine with the concurrency picture: one waiter, many tables).
- [ ] **Step 6: Commit** `feat: add engine core with dependencies, retries, pools and overlap`.

### Task 15: Operator actions, backfill and reload

**Files:**
- Create: `src/pyscheduler/engine/actions.py`, `docs/concepts/operator-actions.md`
- Modify: `src/pyscheduler/engine/core.py`, adding thin `Engine` methods that delegate to `actions.py`
- Test: `tests/engine/test_actions.py`

**Interfaces:**
- Produces:
  - `ActionError(Exception)` with `status: int` (404 unknown, 409 illegal state, 400 bad request)
  - `Engine` methods, all `async`, all recording an `Event(actor=actor, action=<name>, detail=json{before, after, ...})`:
    - `trigger(job: str, *, run_date: date | None = None, actor: str) -> Run`: the run date defaults to today in the job's time zone; `Trigger.MANUAL`
    - `rerun(run_id, *, downstream: bool = False, actor) -> list[Run]`: final runs only. Each run gets `rerun = attempt` and goes back to `scheduled`, then is re-evaluated. With `downstream`, the final runs of `dependents` for the same run date are reopened too, transitively.
    - `hold(run_id, *, actor) -> Run`: from waiting, blocked, queued, scheduled or retry_wait. It removes the run from the ready queue and cancels the retry timer.
    - `release(run_id, *, actor) -> Run`: returns to `held_from`, then re-evaluates.
    - `kill(run_id, *, actor) -> Run`: running only. It sets the operator-kill flag, calls `terminate`, then `kill` after `kill_grace`; the outcome is KILLED and there is no retry.
    - `force_start(run_id, *, actor) -> Run`: from waiting, blocked, held or retry_wait. It goes straight into the ready queue, ignoring dependencies, but not pools or the cap.
    - `skip`, `mark_success`, `mark_failed(run_id, *, actor) -> Run`: non-running runs only. They set the final state and re-evaluate the dependents.
    - `backfill(job, *, start: date, end: date, actor) -> list[Run]`: follows the spec rule, with the first firing as `scheduled_at`; `Trigger.BACKFILL`.
    - `reload(new_config: Config, *, actor) -> None`: stores a `ConfigRecord` with the next version and applies the spec's reload rules (adopt, cancel removed or disabled, keep running attempts, re-evaluate everything, `admission.set_limits`, rebuild the firing heap).

- [ ] **Step 1: Failing tests:**
  - `test_rerun_downstream`: chain a→b, both succeeded; `rerun(a, downstream=True)` reopens both, a runs, then b runs again. The rerun gets the full retry budget again.
  - `test_rerun_non_final_409`.
  - `test_hold_release_returns_state`: hold a waiting run, its dependency succeeds (it stays held), release → queued → runs.
  - `test_kill_no_retry`: retries count 3; a kill gives `failed`, reason `killed`, one attempt.
  - `test_force_start_ignores_deps_not_pools`.
  - `test_skip_satisfies_success_dep`: b waits on failed-then-skipped a → b runs.
  - `test_mark_failed_blocks_downstream`.
  - `test_backfill_three_business_days`: a uk-business job from Fri 2026-10-16 to Tue 2026-10-20 gives 3 runs (Fri, Mon, Tue), with an existing run skipped; dependency-only downstream runs are created.
  - `test_reload_adopts_new_definition`: a waiting run, reload changes `command`, and the attempt launches with the new argv.
  - `test_reload_removed_job_cancels_open_runs` (Review Focus 5).
  - `test_reload_running_attempt_keeps_old`: a running attempt finishes with no retry once its job is removed.
  - `test_events_recorded`: every action writes one event with the actor.
  - `test_unknown_run_404`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `operator-actions.md`.
- [ ] **Step 6: Commit** `feat: add operator actions, backfill and reload`.

### Task 16: Recovery, catch-up and retention

**Files:**
- Create: `src/pyscheduler/engine/recovery.py`, `docs/concepts/recovery-and-catch-up.md`
- Modify: `src/pyscheduler/engine/core.py` (`start()` calls recovery and then catch-up; an hourly retention task)
- Test: `tests/engine/test_recovery.py` (fake), `tests/engine/test_crash.py` (real processes)

**Interfaces:**
- Produces:
  - `async recover(engine) -> None`: loads the open runs and the final runs of the last `memory_days` run dates into the book (published as well). For each `running` run's last attempt, it uses `read_status(status_path)`:
    - present → finish the attempt from the status
    - else `<status>.pid` alive (`os.kill(pid, 0)`) → an adopt task polls every `ADOPT_POLL = 2s` (through the clock) until the status appears or the pid dies
    - else → outcome LOST, retry rules apply

    Queued runs are re-queued, and retry_wait timers are re-armed from their last attempt's `ended_at` + `delay_for`.
  - `async catch_up(engine, now: datetime) -> None`: for each enabled job, takes firings in `(last_tick, now]` and applies `catch_up`:
    - `latest`: only the last firing
    - `all`: every firing
    - `none`: no firings

    It uses `Trigger.CATCH_UP` and the normal uniqueness and overlap rules. When `last_tick` is missing (first start), nothing is caught up.
  - `async prune(engine, now: datetime) -> int`: deletes the final runs with `run_date < (now - retention).date()` in batches of `PRUNE_BATCH = 500` through `Batch.deleted_run_ids`, and evicts final runs older than `memory_days` from the book.
  - Ruling carried from the spec: liveness is a pid check only (`os.kill(pid, 0)`). The spec's start-time match is dropped as unportable, and the docs note the small pid-reuse risk.

- [ ] **Step 1: Failing tests:**
  - `test_recover_finishes_from_status`: a store row is `running` and its status file exists with exit 0 → after `start()` the run is succeeded and its downstream starts.
  - `test_recover_lost_retries`: no status file, a dead pid (use the pid of a finished `subprocess.run`) → attempt `lost`; with retries ≥ 1 the run is re-queued.
  - `test_catch_up_policies`: 3 missed hourly firings since `last_tick` → latest gives 1 run, all gives 3, none gives 0.
  - `test_no_duplicate_after_restart`: a firing processed before stop and replayed after start is not created twice.
  - `test_prune_deletes_old_final_only`.
  - `test_restart_adopts_live_minder` (real, Review Focus 1, in `test_crash.py`):
    - start a real engine (SystemClock, MinderLauncher) with a job `sleep 1; exit 0`
    - once it is running, `stop()` without killing it
    - start a new Engine on the same `state_dir`
    - the run becomes succeeded within 5 s, with exactly one attempt
  - `test_sigkill_scheduler_mid_run` (real):
    - a child Python process runs `serve`-like engine code (a helper script in `tests/engine/_crash_child.py`) and starts 3 jobs of `sleep 1`
    - the parent SIGKILLs the child once all 3 are running, then restarts the engine in-process
    - all 3 runs succeed and none has a second attempt
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Docs:** `recovery-and-catch-up.md` (waking up after a nap and checking the notes the babysitter left).
- [ ] **Step 6: Commit** `feat: add recovery, catch-up and retention`.

### Task 17: HTTP API

**Files:**
- Create: `src/pyscheduler/api/{__init__,auth,serialize,app}.py`
- Test: `tests/api/test_auth.py`, `tests/api/test_app.py` (pyweb `TestClient`, with the engine on a fake clock)

**Interfaces:**
- Produces:
  - `TokenTable.load(path: Path) -> TokenTable`: YAML (yamlish) list of `{name, token_sha256, role}`, with role `viewer` or `operator`.
  - `TokenTable.lookup(token: str) -> tuple[str, str] | None`: compares hashes with `hmac.compare_digest`.
  - `token_middleware(tokens: TokenTable | None) -> Middleware`:
    - with no table, every request is `("dev", "operator")`
    - a missing or unknown token gets 401 JSON `{"error": "unauthorised"}`
    - the role is checked per route (`viewer` for GET, `operator` for POST); a wrong role gets 403
  - `build_app(engine: Engine, *, tokens: TokenTable | None, reload: Callable[[], Awaitable[list[Problem]]], validate: Callable[[], Awaitable[list[Problem]]]) -> App`: the routes in the spec's API list, exactly. Run ids are a single segment.
  - JSON errors are `{"error": str, "problems": [...]?}` with 400, 404 or 409 from `ActionError.status`.
  - `serialize.py`: `run_json(run) -> dict`, `attempt_json`, `event_json`, `job_json(job, next_firings)`, `forecast_json(entry)`, `problem_json(p)`. Datetimes are ISO with `Z`, dates are ISO.
  - `GET /api/runs` reads only `book.published_runs` (plus `store.query_runs` on a worker thread for run dates outside memory).
  - The log endpoint reads the last `tail` lines (default 200, max 5000) of the attempt's file in `run_in_thread`.
  - Blocking store reads use `blocking=True` routes or `run_in_thread`.

- [ ] **Step 1: Failing tests:**
  - `test_health_no_auth_needed`.
  - `test_tokens`: unknown → 401; viewer POST → 403; operator POST → 200.
  - `test_dev_mode_without_tokens`.
  - `test_list_runs_filters`: run_date, job and state.
  - `test_run_detail_has_attempts_and_events`.
  - `test_actions_map_errors`: rerun of a running run → 409, unknown → 404.
  - `test_trigger_and_backfill`.
  - `test_log_tail`.
  - `test_forecast_endpoint`.
  - `test_reload_invalid_returns_problems`: 400 with a problems list; the old config is still active.
  - `test_validate_endpoint`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Commit** `feat: add JSON HTTP API with token auth`.

### Task 18: Service wiring, client and CLI

**Files:**
- Create: `src/pyscheduler/service.py`, `src/pyscheduler/client.py`, `src/pyscheduler/cli.py`, `src/pyscheduler/__main__.py`
- Test: `tests/test_service.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `async serve(config_path: Path, *, ready: Callable[[int], None] | None = None, stop: pynet.loop.Event | None = None) -> None`:
    - loads the config; raises `ConfigError` before opening anything
    - opens the store, stores the config version if its digest changed, and builds `Engine(SystemClock(), MinderLauncher())`
    - starts the `AsyncServer` on `settings.listen` (port 0 is allowed; `ready(port)` is called once listening)
    - SIGTERM and SIGINT end it gracefully (`engine.stop()`, server close); SIGHUP reloads
    - signals are hooked with `signal.signal` → `loop.call_soon_threadsafe`
  - `ApiClient(url: str, token: str | None)`: a method per API call, using stdlib `urllib`. It raises `ApiError(status, message, problems)`.
  - `main(argv: Sequence[str] | None = None) -> int`: the subcommands exactly as in the spec's CLI list. `--url` defaults to the `PYSCHED_URL` env or `http://127.0.0.1:8470`, and `--token` to `PYSCHED_TOKEN`. Exit codes: 0 ok, 1 server error, 2 usage or validation error. `validate` prints each problem through `str(Problem)`, and warnings go to stderr. `status` prints an aligned table: job, run id, state, attempt, started, duration.
  - `pysched hash-token` prints a new random token and its sha256 for the tokens file. Ruling: this is added for usability and costs nothing.

- [ ] **Step 1: Failing tests:**
  - `test_validate_ok_and_errors`: exit 0, or exit 2 with `file:line:` lines.
  - `test_forecast_offline`.
  - `test_serve_end_to_end`:
    - run `serve` in-process on port 0 with a config of a→b (`true` commands)
    - from a worker thread, the client calls `trigger a`, then polls `status` until both runs succeeded (within 5 s)
    - then set `stop`
  - `test_cli_against_server`: `rerun`, `hold` and `release` through `main([...])` return 0; an unknown run gives 1 with the message.
  - `test_serve_refuses_bad_config`.
- [ ] **Step 2: Run.** Expect: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect: PASS.
- [ ] **Step 5: Commit** `feat: add serve, API client and pysched CLI`.

### Task 19: Concurrency proof, example, and docs pass

**Files:**
- Create: `tests/test_concurrency.py`, `examples/etl.yaml`, `examples/scripts/{extract,transform,load,flaky}.sh`, `docs/concepts/` index entries
- Modify: `README.md`, `docs/index.md`, `mkdocs.yml`

- [ ] **Step 1: Write the tests:**
  - `test_two_hundred_jobs` (real):
    - 200 manual jobs `sleep 0.2`, `max_running: 200`; trigger them all
    - they all succeed in under 6 s wall time
    - `max(launcher-observed concurrent) <= 200`
    - a loop-lag probe (a 10 ms repeating `call_later` that records lateness) shows a maximum lateness under 50 ms (`LAG_LIMIT = 0.05`)
  - `test_cap_respected_real`: 50 jobs with `max_running: 8`; the running count observed via `subscribe` never goes above 8.
- [ ] **Step 2: Run** them and fix any engine bottleneck they expose. Expect: PASS. If the lag limit fails on CI, find the blocking call and fix it (move it to `run_in_thread`). Never raise the limit.
- [ ] **Step 3: Write the example.** `examples/etl.yaml` is a small daily chain on a calendar:
  - one on-failure cleanup job
  - one flaky job with retries
  - a pool
  - `previous_business_day`

  The scripts are short shell scripts. README sections cover: what it is, quick start (`pysched validate examples/etl.yaml`, `pysched serve …`, `pysched status`), config reference (link to the docs), concepts list, and the phase roadmap.
- [ ] **Step 4: Docs pass:**
  - `docs/index.md` gets a learning path linking every concept page in order: scheduler, yaml, cron, calendars, time zones, dependencies, runs and states, retries, pools, minder, group commit, recovery, validation, operator actions, forecast
  - `mkdocs.yml` nav is complete
  - `uv run --with mkdocs-material mkdocs build --strict` passes
- [ ] **Step 5: Full gate:** `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` are all clean, and coverage is ≥ 80 %.
- [ ] **Step 6: Commit** `test: prove concurrency; docs: example and learning path`.

### Task 20: PyStack `sched` command (after the v0.1.0 tag)

Done by the controller after merge and tag, in a PyStack worktree (`../pystack-sched-wt`), following the pattern of `plugins/pydb_plugin.py` and `mq_plugin.py`:

- add the `pyscheduler` dependency pinned to `tag = "v0.1.0"`
- add `plugins/sched_plugin.py` registered as `sched`, doing what the spec's PyStack phase 1 bullet says (fixed run date `2026-10-19`)
- add the test `tests/plugins/test_sched_plugin.py`, which asserts the output contains each job's `succeeded` and `history survived`
- deterministic, under 5 s
- PR, CI green, merge
