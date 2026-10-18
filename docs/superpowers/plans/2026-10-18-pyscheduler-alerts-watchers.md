# PyScheduler Phase 2 (Alerts and File Watchers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The scheduler raises alerts (failed, retrying, never started, too
long, too short, lost, blocked, reload failed, watcher timeout), records
them durably, routes them through pymq queues to pluggable notifiers (log,
command, webhook, Slack, Telegram, email) with retries and dead letters,
and runs file-watcher jobs that wait for files to arrive.

**Architecture:**
- Config grows `notifiers:`, `routes:`, typed job `alerts:` and a
  `type: file_watcher` job kind.
- An `AlertMonitor` watches the engine through two after-commit hooks (runs and
  attempts) plus `Clock` timers, writes each alert and its pending
  deliveries through the engine's group-commit `StoreWriter`, then hands
  them to a `Dispatcher`.
- The `Dispatcher` owns an in-process pymq `Broker` with one work queue
  per notifier. pymq leases, retries with backoff and dead-letters. Every
  delivery outcome is written back to the store.
- File watchers are a job kind the engine runs without the launcher.
  A `WatcherHandle` polls `glob` + `stat` in worker threads (with a shared
  limit) on the engine's `Clock`. It takes no global slot, and its matched
  paths are stored and handed to direct dependants.

**Tech Stack:**
- Python 3.14
- pynet v0.1.0 (`pynet.loop`, `pynet.process`)
- pymq v0.3.1 (`pymq.broker.Broker`, `QueueConfig`, `topics.topic_matches`/`validate_pattern`)
- pyweb v0.2.0
- pydb v0.4.0
- stdlib (`urllib.request`, `smtplib`, `glob`, `importlib.metadata`)

**Spec:** `docs/superpowers/specs/2026-10-17-pyscheduler-design.md` (Phase 2 section, plus the Docs, PyStack and Testing sections' phase 2 lines).

Engine facts this plan relies on (file:line as of v0.1.1) are summarised in each task's Interfaces block. Read the named files before changing them.

## Global Constraints

- Python ≥ 3.14; uv; ruff with the repo's rule set; pyright strict on `src` and `tests`; pytest coverage ≥ 80 % and `--timeout=10` per test.
- No `TYPE_CHECKING`, no `from __future__ import annotations`.
- Files stay under about 400 lines. `store/store.py` (409) and `engine/base.py` (396) are already at the limit: new code goes in new modules.
- Every public module, class and function has a docstring in imperative mood (Google style), and so do test classes and methods. Use named constants instead of magic numbers (PLR2004).
- Runtime dependencies are only pynet, pymq, pyweb and pydb, pinned by git tag in `[tool.uv.sources]`, plus the stdlib.
  - The pymq pin is **`v0.3.1`**, the same as PyStack. That pin was dropped in v0.1.1 because the old `v0.2.0` clashed.
- Names (jobs, calendars, pools, notifiers): 1–64 chars of `[A-Za-z0-9_-]`. The job name `_scheduler` is reserved (validation error).
- Instants are aware UTC `datetime`s in the engine, the alerts and the store.
- The engine's timers go through `Clock.call_at` / `Clock.sleep_until` only, wrapped in the engine's `_guard` where they live in the engine. Blocking work (file `stat`, HTTP, SMTP, subprocess) runs in `pynet.loop.run_in_thread`, never on the loop.
- Store failures are fail-stop (phase 1 ruling R11). Alerts are written durably **before** any notifier is called.
- `${ENV}` values never appear in API output, logs, or stored error text. Redact them with the config's `expansions`, as phase 1 does (R23/R29).
- Defaults (exact):
  - notifier `timeout: 10s`
  - notifier `retries: {count: 5, delay: 10s, max_delay: 10m}`
  - watcher `min_age: 30s`, `poll: 10s`, `give_up_after: 1d`
  - scheduler `watch_limit: 8`
  - alert kind `retrying` off unless enabled; every other kind on
- Exact identifiers:
  - topic: `alert.<kind>.<criticality>.<job>`, where `<job>` is `_scheduler` for scheduler-wide alerts and criticality is lowercase
  - alert id: `f"{run_id}:{kind}:{attempt}"`, or `f"_scheduler:reload_failed:{raised_at:%Y%m%dT%H%M%S%fZ}"`
  - delivery id: `f"{alert_id}|{notifier}"`
  - notifier queue: `f"notify.{name}"`
  - delivery statuses: `pending`, `sent`, `dead`
  - entry point group: `pyscheduler.notifiers`
- Every concept page goes in `docs/concepts/<name>.md`. Each is kid-friendly, with an everyday analogy and a runnable snippet, and is added to the `mkdocs.yml` nav and to `docs/index.md`. Doc snippets are executed by `tests/test_doc_snippets.py`; mark any that cannot run with `<!-- no-run -->`.
- Commits are conventional and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Rulings (where this plan refines the spec)

- **P2-R1, routing:** the dispatcher computes each alert's notifiers itself, using pymq's `topic_matches` over the routes, and `put`s one message per notifier queue (id = delivery id). It does not `bind` queues to topics.
  - Why: deliveries must be recorded per notifier *before* sending, so that a restart re-sends only the unsent ones (the spec's "pending alerts are published again", made exact).
  - The alert is also `publish`ed on its topic, for live subscribers (phase 3 SSE).
- **P2-R2, `must_start_by` deadline:** a clock time means that time on the **local date of the run's `scheduled_at`** in the job's time zone, not on the run date. A `previous_business_day` job firing at 02:00 on the 17th for run date 16 with `must_start_by: "03:00"` is due at 03:00 on the 17th.
  - Its timer is armed only for a run's first life (`run.attempt == 0`) and only if `created_at` is before the deadline. So catch-up, backfill and reruns never raise floods of past-due alerts.
  - After a restart, an open never-started run whose deadline passed during the downtime alerts at once.
- **P2-R3, command notifier:** it runs through `pynet.process.create_process(..., stdin=True)` with the alert JSON on stdin, and is killed (`kill_group`) at its timeout.
- **P2-R4, watcher give-up:** ends the attempt with outcome `timeout` and reason `f"gave up: no stable file after {format_duration(give_up_after)}"`. The normal retry policy applies. Each such attempt raises `watcher_timeout`. If the run then ends failed, `failed` is raised too; routes can filter.
- **P2-R5, watcher success:** a poll succeeds when at least one match has had the same `(size, mtime_ns)` for `min_age`. The run's files are every match stable at that poll, sorted.
- **P2-R6, watchers take no global slot:** watchers forbid `pools`, `command`, `cwd`, `env`, `timeout`, `stdout` and `stderr`. Their polls share a `Semaphore(watch_limit)`.
- **P2-R7, matched files:** downstream gets `PYSCHED_FILES` (newline-separated) from every direct `file_watcher` upstream. The `{files}` placeholder (`shlex.join` of those paths) is valid only in jobs that depend directly on a watcher. The paths are stored in a new `run_files` table.
- **P2-R8, recovery:** a watcher attempt `running` at a crash resumes polling with its original `started_at`, so the give-up deadline holds. It is not `lost`.
- **P2-R9, alert dedupe:** an in-memory set of alert ids, seeded at start from `store.alert_ids_since(memory start)` and pruned hourly to runs still in the run book. Store writes of alerts are upserts, so a duplicate can never fail-stop the engine.
- **P2-R10, notifier lookup:** notifier lookup is lazy. Each delivery uses the notifier definition in `engine.config` at send time, so reloads need no hook. A delivery whose notifier was removed goes `dead` with error `notifier removed`.
- **P2-R11, dead letters and replay:** dead letters live in the store (status `dead`), because the broker forgets on restart. Replay sets the delivery to `pending` with `attempts: 0` and queues it again.

## Review Focus

1. **A webhook/Slack/Telegram URL taken from `${SLACK_URL}` that fails.**
   `urllib` errors quote the URL. The stored `last_error`, the logs and
   `GET /api/alerts` must show `${SLACK_URL}`, never the secret (Task 5
   `test_failed_delivery_error_is_redacted`).
2. **The scheduler restarts while a delivery is mid-retry.** After the
   restart it is sent, it is never lost, and notifiers that had already
   succeeded are not sent it again (Task 5 `test_restart_resends_only_pending`).
3. **A `previous_business_day` job with `must_start_by: "03:00"`.** It
   alerts only if it has not started by 03:00 on the day it fired, and a
   backfill of last month raises no never-started alerts (Task 6
   `test_never_started_uses_firing_day` and
   `test_backfill_raises_no_never_started`).
4. **A file still being written** (size grows at every poll). It is not
   matched until it has stopped changing for `min_age`. A file that
   appears only after several polls is then found (Task 9
   `test_growing_file_waits_until_stable`).
5. **The scheduler is killed while a watcher polls.** After the restart
   the watcher resumes, keeps its original give-up deadline, and is not
   `lost` (Task 10 `test_restart_resumes_watcher`).

---

### Task 1: Alert config — notifiers, routes and job alerts

**Files:**
- Modify: `pyproject.toml` (add `pymq` dependency, source `{ git = "https://github.com/robcox2910/pymq", tag = "v0.3.1" }`), `uv.lock`
- Modify: `src/pyscheduler/config/model.py`: add `AlertKind`, `DEFAULT_OFF`, `NotifyRetry`, `NotifierDef`, `Route` and `JobAlerts`; change the type of `JobDef.alerts`; add `Config.notifiers` and `Config.routes`; extend `__all__` and the freeze list
- Create: `src/pyscheduler/config/alert_fields.py` (readers for `notifiers:`, `routes:`, job `alerts:`)
- Create: `src/pyscheduler/config/validate_alerts.py` (semantic checks, called from `validate_config`)
- Modify: `src/pyscheduler/config/loader.py` (add `notifiers` and `routes` to `MAIN_KEYS` only, not `INCLUDED_KEYS`; call the readers in `build_config`), `config/job_fields.py` (`_read_alerts` returns the raw mapping as now; `build_job` converts the merged mapping to `JobAlerts`), `config/__init__.py` (exports)
- Modify: `src/pyscheduler/api/serialize.py`: `definition_json` shows `alerts` as `{"on": [...], "off": [...], "notify": [...]}` for operators and `"***"` otherwise, as now
- Test: `tests/config/test_alert_config.py`

**Interfaces:**
- Consumes:
  - `Reader.add(line, message, severity="error")`
  - `check_keys(r, node, allowed, where) -> set[str]`
  - `read_str` / `read_plain` / `read_duration` / `read_int` / `read_str_list` (config/fields.py)
  - `_Loader.main_section(key)` (loader.py:140)
  - `_Checker.add(where, message, job, severity)` (validate.py:114)
  - `values.check_name`
  - `pymq.broker.topics.validate_pattern(pattern)`, which raises `InvalidTopicError`
- Produces (config/model.py, all frozen dataclasses):
  - `class AlertKind(StrEnum)` with FAILED="failed", RETRYING="retrying", NEVER_STARTED="never_started", TOO_LONG="too_long", TOO_SHORT="too_short", LOST="lost", BLOCKED="blocked", RELOAD_FAILED="reload_failed", WATCHER_TIMEOUT="watcher_timeout"
  - `DEFAULT_OFF: frozenset[AlertKind] = frozenset({AlertKind.RETRYING})`
  - `NotifyRetry(count: int = 5, delay: timedelta = timedelta(seconds=10), max_delay: timedelta = timedelta(minutes=10))`
  - `NotifierDef(name: str, type: str, options: Mapping[str, object], retries: NotifyRetry, timeout: timedelta, source: SourceRef)`: `options` holds every key except `type`, `retries` and `timeout`, `${ENV}`-expanded
  - `Route(match: str, notify: tuple[str, ...], source: SourceRef)`
  - `JobAlerts(on: frozenset[AlertKind] = frozenset(), off: frozenset[AlertKind] = frozenset(), notify: tuple[str, ...] = ())` with `def enabled(self, kind: AlertKind) -> bool` (on wins; otherwise not in `off` and not in `DEFAULT_OFF`)
  - `JobDef.alerts: JobAlerts`
  - `Config.notifiers: Mapping[str, NotifierDef]`, default empty
  - `Config.routes: tuple[Route, ...]`, default `()`
  - `NOTIFIER_TYPES: frozenset[str]` (in alert_fields.py) = `{"log", "command", "webhook", "slack", "telegram", "email"}`, plus names from the `pyscheduler.notifiers` entry points (`importlib.metadata.entry_points(group=...)`)

YAML shape:

```yaml
notifiers:
  team-log: {type: log, path: logs/alerts.log}
  ops-slack: {type: slack, webhook_url: "${SLACK_URL}", retries: {count: 3}, timeout: 5s}
routes:
  - {match: "alert.*.critical.#", notify: [ops-slack]}
  - {match: "alert.failed.#", notify: [team-log]}
jobs:
  load:
    command: "true"
    alerts: {on: [retrying], off: [too_short], notify: [team-log]}
```

- [ ] **Step 1: Write the failing tests**:
  - `test_reads_notifiers_and_routes`: the YAML above loads (with `env={"SLACK_URL": "https://hooks.example/abc"}`).
    - `config.notifiers["ops-slack"].type == "slack"`
    - `.options == {"webhook_url": "https://hooks.example/abc"}`
    - `.retries == NotifyRetry(count=3)`
    - `.timeout == timedelta(seconds=5)`
    - `config.routes[0] == Route("alert.*.critical.#", ("ops-slack",), ANY_SOURCE)`, compared by `match` and `notify`
  - `test_notifier_defaults`: `team-log` has `NotifyRetry()` and `timedelta(seconds=10)`.
  - `test_job_alerts`:
    - `load`'s `alerts.enabled(AlertKind.RETRYING)` is True and `alerts.enabled(AlertKind.TOO_SHORT)` is False
    - a job without `alerts` has `enabled(RETRYING) is False` and `enabled(FAILED) is True`
    - `defaults: {alerts: {notify: [team-log]}}` reaches every job
  - `test_alert_config_errors`, table-driven, one problem each, message contains:
    - unknown notifier type `pager` → `"unknown notifier type 'pager'"`
    - a bad notifier name `bad name` → the name-rule message
    - a route `notify: [nobody]` → `"unknown notifier 'nobody'"`
    - a route `match: "alert..x"` → `"invalid route pattern"`
    - a route without `notify` → `"missing required key 'notify'"`
    - job `alerts: {off: [late]}` → `"unknown alert kind 'late'"`
    - job `alerts: {notify: [nobody]}` → `"unknown notifier 'nobody'"`
    - `alerts: {loud: true}` → the unknown-key message for `loud`
    - a job named `_scheduler` → `"job name '_scheduler' is reserved"`
    - `notifiers:` in an included file → `"notifiers is only allowed in the main file"`
    - `retries: {count: -1}` → the minimum message
  - `test_route_pattern_warning`: `match: "job.#"` loads with a warning containing `"never matches an alert topic"`. A pattern matches alert topics if `topic_matches(pattern, "alert.failed.normal.x")` holds for some kind/criticality/job; test that the first word is `alert`, `*` or `#`.
  - `test_notifier_secret_is_redacted_in_errors`: `ops-slack: {type: slack, webhook_url: "${SLACK_URL}", timeout: nope}` with an env value of 4+ chars. No problem message contains the value. This is already guaranteed by the loader's `_redacted`; the test pins it for the new section.
- [ ] **Step 2: Run** `uv run pytest tests/config/test_alert_config.py -v`. Expected: FAIL (`AlertKind` not defined).
- [ ] **Step 3: Implement.**
  - Add the pymq dependency with `uv lock`.
  - Model types as in Produces.
  - Readers in `alert_fields.py` (`read_notifiers(r, node) -> dict[str, NotifierDef]`, `read_routes(r, node) -> tuple[Route, ...]`, `to_job_alerts(r, raw: Mapping[str, object] | None, line: int) -> JobAlerts`).
  - Checks in `validate_alerts.py` (`check_alerts(checker, config) -> None`, called from `_Checker.run`).
  - Per-type option checks are **not** done here. Task 3 adds them through the notifier registry.
- [ ] **Step 4: Run** `uv run pytest tests/config -q`. Expected: PASS, with the existing config tests unchanged.
- [ ] **Step 5: Commit** `feat: read and validate notifiers, routes and job alerts`.

### Task 2: Alert records, routing and the store tables

**Files:**
- Create: `src/pyscheduler/alerts/__init__.py`, `src/pyscheduler/alerts/model.py`, `src/pyscheduler/alerts/routing.py`
- Modify: `src/pyscheduler/store/schema.py`: add tables `alerts`, `deliveries` and `run_files`, indexes, row helpers, and new `Batch` fields
- Create: `src/pyscheduler/store/alert_store.py`, holding alert reads and writes as functions the `Store` calls (store.py is at its size limit)
- Modify: `src/pyscheduler/store/store.py`: `apply` writes the new batch parts; `_delete_run` also deletes the run's alerts, their deliveries and its `run_files` row; thin read methods delegate to `alert_store`
- Modify: `src/pyscheduler/engine/retention.py`: prune also deletes run-less alerts (and their deliveries) raised before the retention cutoff, in `PRUNE_BATCH` batches
- Modify: `src/pyscheduler/values.py`: add `format_duration(value: timedelta) -> str`, the inverse of `parse_duration` (`timedelta(hours=1, minutes=30)` → `"1h30m"`, 90 s → `"1m30s"`, 4 h → `"4h"`, 1 d → `"1d"`, 0 → `"0s"`)
- Test: `tests/alerts/test_routing.py`, `tests/store/test_alert_store.py`, `tests/test_values.py`

**Interfaces:**
- Consumes:
  - `Batch` (schema.py:109) with `merge` / `is_empty`
  - `Store.apply` (store.py:168)
  - `Store._delete_run` (store.py:201)
  - `TABLES` / `INDEXES` (schema.py:32/87)
  - `Config.routes`, `JobDef.alerts`, `JobDef.criticality`
  - `pymq.broker.topics.topic_matches`
- Produces:
  - `alerts/model.py`:
    - `class DeliveryStatus(StrEnum)`: PENDING="pending", SENT="sent", DEAD="dead"
    - `AlertRecord(alert_id: str, kind: AlertKind, run_id: str | None, job: str | None, run_date: date | None, attempt: int, criticality: Criticality, raised_at: datetime, message: str, detail: Mapping[str, JsonValue])`, frozen
    - `DeliveryRecord(alert_id: str, notifier: str, status: DeliveryStatus, attempts: int, last_error: str, updated_at: datetime)`, frozen, with property `delivery_id -> str` (`f"{alert_id}|{notifier}"`)
    - `SCHEDULER_WORD = "_scheduler"`
    - `def alert_id_for(run_id: str, kind: AlertKind, attempt: int) -> str`
  - `alerts/routing.py`:
    - `def alert_topic(alert: AlertRecord) -> str`
    - `def destinations(config: Config, alert: AlertRecord) -> tuple[str, ...]`: sorted and unique. It is the union of every route whose `match` matches the topic, plus the job's `alerts.notify` when the alert has a job that is still in the config.
    - `def alert_json(alert: AlertRecord, *, scheduler: str) -> dict[str, JsonValue]`, with keys `alert_id, kind, criticality, job, run_id, run_date, attempt, raised_at, message, scheduler, detail`. `raised_at` is ISO ending `Z`; `criticality` is lowercase.
  - `store/schema.py`:
    - `Batch.alerts: list[AlertRecord]`, `Batch.deliveries: list[DeliveryRecord]` and `Batch.run_files: dict[str, tuple[str, ...]]`, each defaulting to empty
    - `Batch.deleted_alert_ids: list[str]`
    - `merge` keeps the last record per `alert_id` / `delivery_id` / run id
  - Tables:
    - `alerts`: `alert_id` TEXT PK, kind, run_id (nullable), job (nullable), run_date DATE (nullable), attempt INT, criticality TEXT, raised_at TIMESTAMP, message, detail TEXT JSON
    - `deliveries`: `delivery_id` TEXT PK, alert_id, notifier, status, attempts INT, last_error, updated_at TIMESTAMP
    - `run_files`: `run_id` TEXT PK, files TEXT JSON list
    - Indexes on `alerts.run_id`, `alerts.raised_at`, `deliveries.alert_id` and `deliveries.status`
    - Writes are upserts by primary key. No `SCHEMA_VERSION` bump: `Store.open` creates missing tables.
  - `Store` reads, all sync and called through `run_in_thread`:
    - `get_alert(alert_id) -> AlertRecord | None`
    - `alerts_for(run_id) -> list[AlertRecord]`
    - `query_alerts(*, kind: AlertKind | None = None, job: str | None = None, since: datetime | None = None, limit: int = 500) -> list[AlertRecord]`, newest first
    - `deliveries_for(alert_ids: Collection[str]) -> dict[str, list[DeliveryRecord]]`
    - `deliveries_with(status: DeliveryStatus, limit: int = 500) -> list[DeliveryRecord]`, oldest first
    - `alert_ids_since(when: datetime) -> set[str]`
    - `runless_alert_ids_before(when: datetime, limit: int) -> list[str]`
    - `run_files(run_ids: Collection[str]) -> dict[str, tuple[str, ...]]`

- [ ] **Step 1: Write the failing tests**:
  - `test_alert_topic`: a `failed` alert for job `load` with criticality CRITICAL → `"alert.failed.critical.load"`. A `reload_failed` alert with no job → `"alert.reload_failed.high._scheduler"`.
  - `test_destinations_union_sorted_unique`: with the Task 1 routes plus job `alerts.notify: [team-log]`, a critical failed alert → `("ops-slack", "team-log")`. A normal `too_long` alert → `("team-log",)` (from the job only).
  - `test_alert_json_fields`: exact keys and values for one alert.
  - `test_round_trip`: save a `Batch` with one alert, two deliveries and `run_files`; reopen the store. The `get_alert`, `deliveries_for`, `run_files` and `deliveries_with(PENDING)` results are equal to what was saved.
  - `test_upsert_is_idempotent`: applying the same alert twice and a delivery twice (second status SENT) leaves one alert and one delivery with status SENT.
  - `test_prune_deletes_alerts_with_their_run`: a pruned run takes its alerts, deliveries and `run_files` row with it.
  - `test_prune_deletes_old_runless_alerts`: run-less alerts older than `retention` go; newer ones stay.
  - `test_format_duration`, table-driven over the values in Files.
- [ ] **Step 2: Run** `uv run pytest tests/alerts tests/store tests/test_values.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: store alerts, deliveries and matched files`.

### Task 3: Notifier protocol, registry, and the log and command notifiers

**Files:**
- Create: `src/pyscheduler/notifiers/__init__.py`, `notifiers/base.py`, `notifiers/local.py`
- Modify: `src/pyscheduler/config/validate_alerts.py`: for each notifier, call `check_notifier(definition, settings)` and report each message as an error at the notifier's source line
- Create: `docs/concepts/notifiers.md`, a kid-friendly page covering all six types. Its HTTP and email sections are written in Task 4. Add it to the `mkdocs.yml` nav and `docs/index.md`.
- Test: `tests/notifiers/test_local.py`, `tests/notifiers/test_registry.py`

**Interfaces:**
- Consumes: `NotifierDef` (the config folder is `Path(definition.source.file).parent`), `Settings` (`log_dir`)
- Produces (`notifiers/base.py`):
  - `class NotifyError(Exception)`: a failed send; its message is safe to store once redacted
  - `class Notifier(Protocol)`: `async def send(self, alert: Mapping[str, JsonValue]) -> None`, which raises `NotifyError` on failure
  - `type NotifierFactory = Callable[[NotifierDef, Settings], Notifier]`; a factory raises `ValueError(message)` for bad options
  - `def build_notifier(definition: NotifierDef, settings: Settings) -> Notifier`: built-in types first, then the `pyscheduler.notifiers` entry point with that name (its loaded object is a `NotifierFactory`)
  - `def check_notifier(definition: NotifierDef, settings: Settings) -> list[str]`: calls `build_notifier` and returns `[str(exc)]` on `ValueError`. Factories must not do I/O.
- Produces (`notifiers/local.py`):
  - `LogNotifier`: option `path` (required). A relative path resolves against the config file's folder. `send` appends one JSON line (`json.dumps(alert, sort_keys=True)`) through `run_in_thread`, creating parent folders.
  - `CommandNotifier`: option `command`, a str (run with `("/bin/sh", "-c", command)`) or a list of str (required). `send` runs it with `pynet.process.create_process(argv, stdin=True, stdout=True, stderr=True)`, writes the alert JSON and closes stdin.
    - It waits up to `definition.timeout`, then `kill_group()` and `NotifyError("command timed out after 5s")`, with the duration via `format_duration`.
    - A non-zero exit gives `NotifyError(f"command exited {code}: {last stderr line}")`.
    - `create_process` needs the loop's child watcher; set it up the way `engine/launcher.py` does.
  - Unknown option keys → `ValueError("unknown option 'x' for log notifier")`.

- [ ] **Step 1: Write the failing tests**:
  - `test_log_notifier_appends_json_lines`: two sends write two lines that parse back to the alerts. The relative `path` resolves next to the config file.
  - `test_command_notifier_gets_alert_on_stdin`: the command is `cat > out.json` (run in `tmp_path`). The file holds the alert JSON.
  - `test_command_notifier_nonzero_exit`: `echo boom >&2; exit 3` raises `NotifyError` with a message containing `"exited 3"` and `"boom"`.
  - `test_command_notifier_timeout_kills_group`: `sleep 30 & sleep 30` with `timeout: 1s` raises `NotifyError("command timed out after 1s")` in under 3 s, and no `sleep 30` child is left (check `os.killpg(pgid, 0)` raises `ProcessLookupError`).
  - `test_registry_unknown_options_and_types`: `check_notifier` for a log notifier without `path` gives `["missing required option 'path' for log notifier"]`, and an extra `colour` gives the unknown-option message. An entry-point type registered through a monkeypatched `entry_points` builds.
  - `test_validation_reports_option_errors`: loading a config whose log notifier lacks `path` raises `ConfigError` at the notifier's line.
- [ ] **Step 2: Run** `uv run pytest tests/notifiers -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: add the notifier registry and the log and command notifiers`.

### Task 4: Webhook, Slack, Telegram and email notifiers

**Files:**
- Create: `src/pyscheduler/notifiers/web.py`, `src/pyscheduler/notifiers/email.py`
- Modify: `src/pyscheduler/notifiers/base.py` (register the four types), `docs/concepts/notifiers.md`
- Test: `tests/notifiers/test_web.py`, `tests/notifiers/test_email.py`

**Interfaces:**
- Consumes: `NotifyError`, `NotifierDef`, the registry
- Produces:
  - `notifiers/web.py`:
    - `async def post_json(url: str, body: JsonValue, *, headers: Mapping[str, str], timeout: float) -> None`. It runs `urllib.request` in `run_in_thread` with `Content-Type: application/json` and does not follow redirects (reuse the `_NoRedirects` idea from client.py).
    - A non-2xx status raises `NotifyError(f"HTTP {status}")`. Network errors raise `NotifyError(str(reason))`.
    - Notifier types and their bodies:

      | Type | Options | Body sent |
      |------|---------|-----------|
      | `WebhookNotifier` | `url` (required), `headers` (str map) | the alert JSON |
      | `SlackNotifier` | `webhook_url` (required) | `{"text": f"[{criticality}] {message}"}` |
      | `TelegramNotifier` | `bot_token`, `chat_id` (both required), `api_url` (default `"https://api.telegram.org"`) | POSTs to `f"{api_url}/bot{bot_token}/sendMessage"` with `{"chat_id": chat_id, "text": f"[{criticality}] {message}"}` |
  - `notifiers/email.py`, `EmailNotifier`:
    - options: `host` (required), `port` (default 587), `from` (required), `to` (str or list of str, required), `username` and `password` (both or neither), `starttls` (default true)
    - `send` runs `smtplib.SMTP(host, port, timeout=...)` in `run_in_thread`: `starttls()` when enabled, then `login` if a username is given, then `send_message`
    - subject `f"[pysched {criticality}] {message}"`; body is `json.dumps(alert, indent=2, sort_keys=True)`
    - `smtplib.SMTPException` / `OSError` become `NotifyError`
- [ ] **Step 1: Write the failing tests.** The HTTP tests use a stdlib `http.server.ThreadingHTTPServer` on `127.0.0.1:0` in a thread that records requests.
  - `test_webhook_posts_alert_json`: the body equals `alert`, the content type is JSON, and the custom header arrives.
  - `test_slack_body`: `{"text": "[critical] load.2026-10-19.1 failed: exit 1"}`.
  - `test_telegram_url_and_body`: path `/botTOKEN/sendMessage` and body `{"chat_id": "42", "text": ...}`, with `api_url` pointed at the test server.
  - `test_http_error_status`: a 500 raises `NotifyError("HTTP 500")`. A 302 is not followed, and raises `NotifyError("HTTP 302")`.
  - `test_http_timeout`: a server that never answers, with `timeout: 1s`, raises `NotifyError` in under 3 s while the loop keeps ticking. Check that a 50 ms `sleep` loop on the event loop advances during the send.
  - `test_email_starttls_login_send`: `smtplib.SMTP` is monkeypatched with a recording fake. It records the calls `starttls`, `login(user, pw)` and `send_message`, the subject is `"[pysched high] ..."`, and `To` holds both addresses.
  - `test_email_option_errors`: `username` without `password` gives `"username and password go together"`, and a missing `to` gives the missing-option message.
- [ ] **Step 2: Run** `uv run pytest tests/notifiers -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: add webhook, Slack, Telegram and email notifiers`.

### Task 5: The dispatcher — pymq queues, retries, dead letters, resume and replay

**Files:**
- Create: `src/pyscheduler/alerts/dispatch.py`
- Create: `docs/concepts/routing-alerts.md`, about the post office with one pigeonhole per notifier. Cover topics, `*` and `#`, retries, dead letters and replay; add it to the nav and index.
- Test: `tests/alerts/test_dispatch.py`, `tests/alerts/alert_helpers.py` (shared: `RecordingNotifier`, `FlakyNotifier(fails: int)`, a factory mapping names to them, and a small engine-plus-store harness reusing `tests/engine/engine_helpers.py`)

**Interfaces:**
- Consumes:
  - `Engine` (attributes `config`, `store`, `writer`, `clock`)
  - `StoreWriter.save(Batch)`
  - `Store.deliveries_with`, `Store.get_alert`
  - `alert_json`, `destinations`, `alert_topic`
  - `NotifierFactory` / `build_notifier`
  - `pymq.broker`: `Broker()`, `declare_queue(name, QueueConfig)`, `put(queue, body, *, id, headers)`, `consume(queue, sink, *, prefetch)`, `ack` / `nack(queue, delivery_tag, *, requeue, error, consumer_tag)`, `publish(topic, body)`, `close()`, `LeasedMessage(queue, consumer_tag, delivery_tag, envelope)`, `Envelope.attempt`
  - Every `Broker` method is synchronous and must be called on the loop thread, inside a running loop
  - `config/redaction.redact_text`, with `Config.expansions`
- Produces `class Dispatcher`:
  - `__init__(self, engine: Engine, *, factory: NotifierFactory = build_notifier, time_scale: float = 1.0)`. `time_scale` multiplies every pymq delay and timeout; tests pass `0.01`.
  - `async start(self) -> None`: creates the `Broker` and re-queues every `PENDING` delivery from the store, oldest first.
  - `def enqueue(self, alert: AlertRecord, deliveries: Sequence[DeliveryRecord]) -> None`: called after the alert is durable. It `publish`es on `alert_topic(alert)` and `put`s one message per delivery on `notify.<notifier>`, declaring the queue on first use. Each message has `id=delivery_id`, the body `alert_json(...)`, and headers `{"alert_id": ..., "notifier": ...}`.
  - `async replay(self, alert_id: str, notifier: str | None = None) -> list[DeliveryRecord]`: only `DEAD` deliveries can be replayed. If there are none, it raises `LookupError("no dead delivery for <alert_id>")`.
  - `async stop(self) -> None`: cancels consumers, waits for in-flight sends up to 5 s, then `broker.close()`.
  - `async settle(self) -> None`, a test helper: returns when no send is in flight and no message is ready or leased.
  - Each queue's `QueueConfig`:
    - `ack_timeout=(timeout + 5 s) × scale`
    - `max_attempts=retries.count + 1`
    - `retry_delay=retries.delay × scale`
    - `max_retry_delay=retries.max_delay × scale`
    - `prefetch=4`
    - When a notifier definition changes on reload, declare a fresh queue name for it, `notify.<name>.<n>` with `n` increasing, because `declare_queue` refuses a changed config.
  - Delivery flow:
    1. The sink spawns `_deliver(msg)`.
    2. Look the notifier up in the current `engine.config`. If it is missing, write `DEAD` with error `"notifier removed"` and `ack`.
    3. `await notifier.send(body)`.
    4. On success, write `SENT` with `attempts=envelope.attempt`, then `ack`.
    5. On `NotifyError` or any `Exception`, write `attempts` and the redacted `last_error`. If `envelope.attempt >= max_attempts`, write `DEAD`; otherwise keep `PENDING`. Then `nack(requeue=True, error=...)`. pymq applies backoff and dead-letters the last one.
    6. A store failure stops delivering, because the engine fail-stops.
    7. Every write goes through `engine.writer.save(Batch(deliveries=[...]))`.
- [ ] **Step 1: Write the failing tests**:
  - `test_delivers_to_each_destination`: two deliveries for one alert reach two `RecordingNotifier`s. Both rows become `SENT` with `attempts == 1`.
  - `test_retries_then_succeeds`: `FlakyNotifier(fails=2)` with `retries: {count: 3}` is sent 3 times and ends `SENT` with `attempts == 3`.
  - `test_dead_after_retries`: always failing with `count: 1` ends `DEAD` after 2 sends, with `last_error` set. It appears in `deliveries_with(DEAD)`.
  - `test_failed_delivery_error_is_redacted`: the notifier raises `NotifyError("cannot reach https://hooks.example/abc")`, where that URL came from `${SLACK_URL}`. The stored `last_error` is `"cannot reach ${SLACK_URL}"`.
  - `test_restart_resends_only_pending`: store one alert with deliveries A `SENT` and B `PENDING`, then `start()` a fresh dispatcher. Only B's notifier receives it.
  - `test_replay_dead`: a dead delivery replayed with a now-healthy notifier ends `SENT`. Replaying a `SENT` one raises `LookupError`.
  - `test_removed_notifier_goes_dead`: after `engine.reload` with a config missing the notifier, a pending delivery ends `DEAD` with error `"notifier removed"`.
  - `test_publishes_alert_topic`: a `broker.subscribe("alert.#", sink)` sees the topic `alert.failed.normal.load`.
  - `test_slow_notifier_does_not_block_others`: one notifier sleeps 2 s and another records. The second receives within 0.5 s.
- [ ] **Step 2: Run** `uv run pytest tests/alerts/test_dispatch.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: dispatch alerts through pymq queues with retries and dead letters`.

### Task 6: The alert monitor — kinds, deadlines, dedupe and recovery

**Files:**
- Create: `src/pyscheduler/engine/hooks.py`: `AttemptHooks`, a mixin or helper owning the attempt listeners, so `base.py` stays under the limit
- Modify: `src/pyscheduler/engine/base.py`:
  - `_transition` calls the attempt listeners for each attempt in the committed batch, after the run listeners
  - expose `watch_attempts`
- Create: `src/pyscheduler/alerts/monitor.py`, plus `src/pyscheduler/alerts/deadlines.py` if `monitor.py` passes about 300 lines
- Create: `docs/concepts/alerts.md`, about the smoke alarm and the teacher who notices you never arrived. Cover every kind, `must_start_by` / `max_runtime` / `min_runtime`, dedupe, and job `alerts:`; add it to the nav and index.
- Test: `tests/alerts/test_monitor.py`, `tests/engine/test_attempt_hooks.py`

**Interfaces:**
- Consumes:
  - `EngineBase.subscribe(callback: Callable[[Run], None])` (base.py:128), called after each commit, once per changed run, with no previous state
  - `EngineBase._transition` (base.py:237)
  - `Clock.call_at(when, cb) -> Cancel`
  - `engine.book.open_runs()`
  - `Store.latest_attempts(run_ids)`
  - `engine.memory_start() -> date`
  - `Run` (state, reason, attempt, rerun, scheduled_at, created_at, run_date)
  - `Attempt` (outcome, started_at, ended_at, attempt)
  - `Outcome.LOST` / `TIMEOUT` / `SUCCEEDED`
  - `JobDef.must_start_by: time | timedelta | None`, `max_runtime`, `min_runtime`, `alerts`, `criticality`, `timezone`
  - `Dispatcher.enqueue`
  - `destinations`
- Produces:
  - `EngineBase.watch_attempts(self, callback: Callable[[Attempt], None]) -> Callable[[], None]`: called after commit for every attempt row written (started and ended), with the same exception handling as `subscribe`
  - `class AlertMonitor`:
    - `__init__(self, engine: Engine, dispatcher: Dispatcher)`: subscribes to both hooks at once, so it must be built *before* `engine.start()` to see recovery's transitions
    - `async start(self) -> None`: seeds dedupe from `store.alert_ids_since(midnight UTC of engine.memory_start())`, arms deadlines for the open runs and running attempts already in memory, and starts the hourly dedupe prune
    - `async stop(self) -> None`: cancels the timers and waits for raise tasks
    - `def reload_failed(self, problems: Sequence[Problem]) -> None`: raises a `RELOAD_FAILED` alert with criticality HIGH and no run, message `f"reload failed: {n} error(s); nothing changed"`, and detail `{"problems": [problem_json(...) ...]}`, redacted
    - `async settle(self) -> None`, a test helper
  - Raising an alert:
    1. Check `job.alerts.enabled(kind)` (scheduler alerts are always enabled).
    2. Dedupe by alert id.
    3. Build the `AlertRecord` and `PENDING` `DeliveryRecord`s for `destinations(...)`.
    4. `await engine.writer.save(Batch(alerts=[a], deliveries=ds))`.
    5. `dispatcher.enqueue(a, ds)`. An alert with no destinations is still stored.
  - Kinds:

    | Kind | When it is raised | Attempt in the id | Message |
    |------|-------------------|-------------------|---------|
    | `failed` | run notified FAILED | `run.attempt` | `f"{run_id} failed: {run.reason}"` |
    | `retrying` | run notified RETRY_WAIT | `run.attempt` | builder's wording |
    | `blocked` | run notified BLOCKED | `run.attempt` | builder's wording |
    | `lost` | ended attempt with outcome LOST | `attempt.attempt` | builder's wording |
    | `too_short` | ended attempt SUCCEEDED with `ended_at - started_at < min_runtime` | `attempt.attempt` | builder's wording |
    | `too_long` | timer at `started_at + max_runtime`, armed on a started attempt (`ended_at is None`) and cancelled when it ends; at fire time the attempt is still running | `attempt.attempt` | builder's wording |
    | `never_started` | timer per P2-R2, armed when a run is first notified non-final with `attempt == 0`, cancelled when it becomes RUNNING or final; at fire time the state is one of scheduled, waiting, blocked, queued or held | `0` | `f"{run_id} has not started by {deadline_local:%H:%M %Z}"` |
    | `watcher_timeout` | ended attempt with outcome TIMEOUT for a `file_watcher` job (added in Task 10) | `attempt.attempt` | builder's wording |

    - The detail of every kind carries the relevant numbers (for example `{"runtime_seconds": ..., "min_runtime_seconds": ...}`).
    - The wording marked "builder's wording" must contain the run id.
    - Timers are wrapped so that an exception is logged, not fail-stopped. A failed alert must not stop jobs.
- [ ] **Step 1: Write the failing tests.** They use FakeClock and FakeLauncher through the engine harness plus a dispatcher with `RecordingNotifier`s, and a route `{match: "alert.#", notify: [rec]}`.
  - `test_attempt_hook_sees_start_and_end`: one run gives two attempt callbacks, the first with `ended_at is None` and the second ended.
  - `test_failed_alert_once`: a job failing with `retries: {count: 1}` raises no `retrying` alert by default and exactly one `failed` alert, id `f"{run_id}:failed:2"`. A rerun that fails again adds `f"{run_id}:failed:3"`.
  - `test_retrying_when_enabled`: with `alerts: {on: [retrying]}`, one `retrying` alert.
  - `test_off_kind_raises_nothing`: `alerts: {off: [failed]}` gives no alert rows at all.
  - `test_too_long_fires_while_running`: `max_runtime: 5m` and `timeout: 10m` with a 7-minute `FakeResult`. After 5 m one `too_long` arrives while the run is still RUNNING. A 3-minute job raises none.
  - `test_too_short`: `min_runtime: 2m` with a 1-minute success gives one `too_short`; a 1-minute failure gives no `too_short`.
  - `test_lost_alert_after_restart`: run the recovery-test pattern where the minder is gone. There is one `lost` alert.
  - `test_blocked_alert`: the upstream fails, so the downstream is blocked, giving one `blocked` alert.
  - `test_never_started_uses_firing_day`:
    - Setup: job `rep` has `schedule: "0 2 * * *"`, `run_date: previous_business_day`, a calendar, `must_start_by: "03:00"`, and depends on a `manual` job that is never triggered.
    - At 02:00 the run is created. At 02:59 there is no alert. At 03:00 one `never_started` alert arrives with message `f"{run_id} has not started by 03:00 UTC"`.
  - `test_never_started_duration`: `must_start_by: 30m` on a held run alerts at `scheduled_at + 30m`. A run that started at +10m never alerts.
  - `test_backfill_raises_no_never_started`: a backfill over the 30 days before today gives zero `never_started` alerts.
  - `test_restart_arms_deadlines`: stop the engine before the deadline, advance the fake clock past it, and start a new engine, monitor and dispatcher on the same store. One `never_started` alert arrives, and a second restart raises no duplicate.
  - `test_reload_failed_alert`: `monitor.reload_failed([problem])` stores an alert with topic `alert.reload_failed.high._scheduler` and a detail holding the redacted problem.
  - `test_store_write_failure_is_fail_stop`: monkeypatch `writer.save` to raise. The engine's `wait_failed` returns, and the monitor stops raising.
  - `test_alert_does_not_delay_jobs`: a notifier sleeping 2 s does not delay a downstream job's start. The downstream is RUNNING at the same fake instant as without alerts.
- [ ] **Step 2: Run** `uv run pytest tests/alerts/test_monitor.py tests/engine/test_attempt_hooks.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: raise alerts for failed, slow, short, lost, blocked and late runs`.

### Task 7: Service wiring, alert API and CLI

**Files:**
- Modify: `src/pyscheduler/service.py` / `service_parts.py`:
  - in `_open_engine`, build `Dispatcher(engine)` and `AlertMonitor(engine, dispatcher)` after the `Engine` and before `engine.start()`
  - after `_start`, await `dispatcher.start()` then `monitor.start()`
  - in `shut_down`, before `engine.stop()`, await `monitor.stop()` then `dispatcher.stop()`
  - a SIGHUP reload that returns errors calls `monitor.reload_failed(problems)`
- Modify: `src/pyscheduler/api/admin.py`: `config_actions(..., on_failed: Callable[[Sequence[Problem]], None] | None = None)`, called with the error problems when `reload` fails, from both the API and SIGHUP paths
- Create: `src/pyscheduler/api/alerts.py`, with `add_alert_routes(app, engine, dispatcher)` called from `build_app` (which gains `dispatcher: Dispatcher | None = None`; with `None` the alert routes return 503 `{"error": "alerts are not running"}`)
- Modify: `src/pyscheduler/api/runs.py`: `GET /api/runs/<id>` gains `"alerts": [alert_json + "deliveries"]`
- Modify: `src/pyscheduler/client.py`, `cli.py` / `cli_commands.py` (or a new `cli_alerts.py` to stay under 400 lines), `cli_format.py`
- Modify: `docs/concepts/http-api.md`, `docs/concepts/cli.md` (or whichever pages hold the endpoint and command lists)
- Test: `tests/api/test_alert_routes.py`, `tests/test_service_alerts.py`, `tests/test_cli_alerts.py`

**Interfaces:**
- Consumes: `Dispatcher` and `AlertMonitor` (Tasks 5–6), `Store.query_alerts` / `deliveries_for` / `deliveries_with` / `alerts_for`, `guarded`, `query_int`, `query_name`, `role_of`, `json_body`
- Produces, the routes. Viewers can read, and `${ENV}` values are redacted in every response.

  | Route | Behaviour | Result |
  |-------|-----------|--------|
  | `GET /api/alerts?kind=&job=&since=&limit=` | default limit 100, max 1000; a bad `kind` gives 400 `"kind must be one of ..."` | `{"alerts": [alert_json + {"deliveries": [delivery_json]}]}` |
  | `GET /api/alerts/dead?limit=` | lists the `DEAD` deliveries | `{"deliveries": [delivery_json + {"alert": alert_json}]}` |
  | `POST /api/alerts/<alert_id>/replay` (operator), JSON body `{"notifier": str}` optional | an unknown alert gives 404; no dead delivery gives 409 `{"error": "no dead delivery for <id>"}` | `{"deliveries": [...]}` |

  - `delivery_json` has the keys `notifier, status, attempts, last_error, updated_at`.
  - `ApiClient.alerts(*, kind=None, job=None, since=None, limit=None)`, `.dead_alerts(limit=None)` and `.replay(alert_id, *, notifier=None)`.
  - CLI commands:
    - `pysched alerts [--kind K] [--job J] [--since DATE]` prints a table with the columns `RAISED KIND CRIT JOB RUN STATUS MESSAGE`. STATUS is `sent`, `pending`, `dead`, or `none` when there are no deliveries; with several notifiers it shows the worst status, where dead beats pending beats sent.
    - `pysched alerts --dead`
    - `pysched replay ALERT [--notifier N]`
- [ ] **Step 1: Write the failing tests**:
  - API tests, with `TestClient` and seeded store rows:
    - list, filters and redaction
    - viewer reading and viewer replaying (403)
    - replay a dead delivery, which becomes pending
    - replay when nothing is dead (409)
    - run detail includes alerts
  - `test_service_alerts_end_to_end`:
    - Setup: `serve` runs a config with notifier `rec: {type: log, path: alerts.log}`, route `alert.#` to `rec`, and a manual job `boom: {command: "exit 1"}`.
    - Trigger `boom`. Within 5 s `alerts.log` has one line with `"kind": "failed"`, and `GET /api/alerts` shows it `sent`.
  - `test_sighup_reload_failure_alerts`: break the config and call the reload path with SIGHUP. A `reload_failed` line appears in `alerts.log`.
  - `test_cli_alerts_and_replay`: against the live in-process server, `alerts` prints the table and `replay` of a dead delivery exits 0. A dead delivery comes from a `command` notifier with `exit 1` and `retries: {count: 0}`.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: serve alerts over the API and CLI and alert on failed reloads`.

### Task 8: File-watcher config

**Files:**
- Modify: `src/pyscheduler/config/model.py`:
  - add `JobKind(StrEnum)` (COMMAND="command", FILE_WATCHER="file_watcher") and `WatchSpec(pattern: str, min_age: timedelta, poll: timedelta, give_up_after: timedelta)`
  - add `JobDef.kind: JobKind` and `JobDef.watch: WatchSpec | None`
  - add `Settings.watch_limit: int`
- Create: `src/pyscheduler/config/watch_fields.py`, and extend `JOB_KEYS` with `type`, `watch`, `min_age`, `poll` and `give_up_after`
- Modify: `config/job_fields.py` (`build_job`; `command` is required only for command jobs, and watchers get `command=()`), `config/loader.py` (`SCHEDULER_KEYS` gains `watch_limit`, default 8, minimum 1), `config/validate.py` / a new `config/validate_watchers.py`, `templates.py` (`PLACEHOLDERS` gains `files`), `api/serialize.py` (`definition_json` shows `type` and the watch keys)
- Test: `tests/config/test_watcher_config.py`

**Interfaces:**
- Consumes: the job readers, `read_template`, `check_placeholders` (validate.py), `PLACEHOLDERS`
- Produces:
  - `JobDef.kind`, `JobDef.watch`, `Settings.watch_limit`, `WatchSpec`
  - `WATCH_PLACEHOLDERS = PLACEHOLDERS - {"attempt", "files"}`, allowed in `watch`
  - the placeholder `files`, valid only in jobs with a direct `depends_on` on a `file_watcher`
- [ ] **Step 1: Write the failing tests**:
  - `test_reads_watcher`: the spec's `trades-file` example loads.
    - `kind == FILE_WATCHER`
    - `watch.pattern == "/data/in/trades_{run_date:%Y%m%d}.csv"`
    - `min_age == 30s`, `poll == 10s`, `give_up_after == 4h`
    - defaults when omitted are 30s, 10s and 1d
  - `test_watcher_errors`, table-driven, message contains:
    - `type: file_watcher` without `watch` → `"missing required key 'watch'"`
    - with `command` → `"file watchers take no command"`
    - each of `cwd`, `env`, `timeout`, `pools`, `stdout`, `stderr` → `"file watchers take no <key>"`
    - `watch` on a command job → `"watch is only for file watchers"`
    - `type: cron` → `"unknown job type 'cron'"`
    - `{attempt}` in `watch` → the unknown-placeholder message
    - `poll: 0s` → the greater-than-zero message
  - `test_files_placeholder_only_for_direct_dependants`: `{files}` in a job depending on the watcher is fine; in a job that does not gives `"unknown placeholder 'files'"`.
  - `test_watch_limit`: `scheduler: {watch_limit: 2}` loads as 2, `0` gives the minimum error, and the default is 8.
- [ ] **Step 2: Run** `uv run pytest tests/config/test_watcher_config.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: read and validate file-watcher jobs`.

### Task 9: Watching for files

**Files:**
- Create: `src/pyscheduler/engine/watcher.py`
- Test: `tests/engine/test_watcher.py`

**Interfaces:**
- Consumes: `Clock` (`now`, `sleep_until`), `pynet.loop.Semaphore`, `run_in_thread`, `AttemptResult(exit_code, signal, started_at, ended_at, start_error)` (engine/status_note.py), the `AttemptHandle` protocol (engine/launcher.py: `pid`, `async wait()`, `terminate()`, `kill()`), `format_duration`
- Produces:
  - `def stat_matches(pattern: str) -> dict[str, tuple[int, int]]`: a sync, thread-side function. It runs `glob.glob(pattern)`, keeps only regular files, and maps each path to `(st_size, st_mtime_ns)`. Files that vanish between glob and stat are skipped.
  - `class WatcherHandle` (implements `AttemptHandle`, `pid` is always `None`):
    - `__init__(self, *, pattern: str, spec: WatchSpec, started_at: datetime, clock: Clock, limit: Semaphore, log_path: Path)`
    - `files: tuple[str, ...]`, set on success
    - `gave_up: bool`
    - `wait()` polls:
      1. immediately, then every `spec.poll`
      2. each poll takes `limit`, then `await run_in_thread(stat_matches, pattern)`
      3. it tracks, per path, the signature and the first `clock.now()` that signature was seen
      4. it succeeds per P2-R5, returning `AttemptResult(exit_code=0, signal=None, ...)`
    - At `started_at + give_up_after`, `wait()` sets `gave_up = True` and returns `AttemptResult(exit_code=None, signal=None, ..., start_error=None)`.
    - `terminate()` / `kill()` stop polling, and `wait()` then returns `AttemptResult(exit_code=None, signal=signal.SIGTERM, ...)`.
    - It appends one line per poll whose matches changed (`f"{now:%H:%M:%S} {n} match(es)"`) and, on success, one `f"found {path}"` line per file to `log_path`, writing in a thread.
- [ ] **Step 1: Write the failing tests.** They use FakeClock and real files in `tmp_path`.
  - `test_finds_stable_file`: with the file present from the start, `min_age: 30s` and `poll: 10s`, `wait()` succeeds after the fake clock reaches +30 s (not at +20 s), with `files == (path,)`.
  - `test_growing_file_waits_until_stable`: the file grows by 1 byte at each of the first 3 polls. Success comes only 30 s after the last change.
  - `test_file_appears_late`: with no match for 5 polls, then the file appears, it is found `min_age` later.
  - `test_glob_returns_all_stable_matches_sorted`: a glob matching `b.csv` and `a.csv` gives `files == (a, b)`. A directory matching the glob is ignored.
  - `test_gives_up`: with `give_up_after: 1m` and no file, `gave_up is True` at +1 m.
  - `test_started_at_in_the_past_keeps_deadline`: `started_at = now - 50s` with `give_up_after: 1m` gives up at +10 s.
  - `test_limit_is_shared`: 5 handles share `Semaphore(2)`. With `stat_matches` patched to count concurrent calls, there are never more than 2 at once.
  - `test_terminate_stops_polling`: `wait()` returns at once with `signal == SIGTERM`.
- [ ] **Step 2: Run** `uv run pytest tests/engine/test_watcher.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: poll for stable files`.

### Task 10: File watchers in the engine

**Files:**
- Modify:
  - `src/pyscheduler/engine/attempts.py`: in `_start_attempt` / `_attempt`, for `kind == FILE_WATCHER`, build a `WatcherHandle` instead of a `LaunchSpec` and `launcher.launch`
    - the Attempt row's `stdout_path` is the default stdout template rendered (the watcher's log); `stderr_path` and `status_path` are `""`
    - on success, `run_files` goes in the final batch
    - a give-up classifies as `Outcome.TIMEOUT` with the P2-R4 reason
  - `src/pyscheduler/engine/admission.py`: `ReadyQueue.push(run, criticality, needs, *, global_slot: bool = True)`, and `Admission.acquire` / `release` / `adopt` take `global_slot`
  - `src/pyscheduler/engine/base.py`: `_queue` passes `global_slot=False` for watchers, and the slot is remembered per run so `_release` gives back the right thing. Put the helper in `engine/hooks.py` or a new module if `base.py` would pass 400 lines.
  - `src/pyscheduler/engine/recovery.py`: a running watcher attempt resumes (P2-R8)
  - `src/pyscheduler/engine/core.py`: the engine owns `watch_limit: Semaphore`, rebuilt on reload when the value changes, and `_files: dict[str, tuple[str, ...]]`, loaded at recovery from `store.run_files` for runs in the book
  - `_launch_spec` adds `PYSCHED_FILES` and the `files` template value (P2-R7)
- Modify: `src/pyscheduler/alerts/monitor.py`: add the `watcher_timeout` kind
- Create: `docs/concepts/file-watchers.md`, about waiting at the letterbox until the parcel stops rustling. Cover `min_age`, globs, `{files}`, give-up, and why no slot is used; add it to the nav and index.
- Test: `tests/engine/test_watcher_engine.py`, `tests/test_watcher_integration.py` (real processes)

**Interfaces:**
- Consumes: `WatcherHandle`, `JobDef.kind` / `watch`, `Settings.watch_limit`, `Batch.run_files`, `Store.run_files`, `book.dependency_run(job, run_date)`, `calendar.shift` (for dependency offsets, as `deps.py` does)
- Produces:
  - `Engine.files_for(run_id: str) -> tuple[str, ...]`, which is `()` when unknown
  - downstream env `PYSCHED_FILES`
  - template value `files`
- [ ] **Step 1: Write the failing tests**:
  - `test_watcher_run_succeeds_and_feeds_downstream`:
    - Setup: watcher `in` on `{tmp}/in_{run_date:%Y%m%d}.csv` with `min_age: 30s` and `poll: 10s`; `load` depends on `in` with `command: "cat {files}"`.
    - Create the file and advance the fake clock.
    - `in` succeeds; the launched spec for `load` has `PYSCHED_FILES == path` and argv contains `shlex.join([path])`.
  - `test_watcher_takes_no_global_slot`: with `max_running: 1`, 3 watchers polling and one command job, the command job is RUNNING at once, and `admission.running == 1`.
  - `test_watcher_gives_up_and_alerts`: `give_up_after: 1m` and no file gives an attempt outcome TIMEOUT, reason `"gave up: no stable file after 1m"`, the run FAILED, and the alerts `watcher_timeout` (attempt 1) and `failed`.
  - `test_watcher_retry`: with `retries: {count: 1, delay: 1m}`, the second attempt finds the file created after the first gave up.
  - `test_kill_watcher`: an operator `kill` ends it `failed`, reason `killed by ...`.
  - `test_restart_resumes_watcher`: stop the engine at +40 s of a 1 m give-up, start a new engine on the same store, and create no file. It gives up at +1 m from the original start, not +1 m 40 s. The outcome is TIMEOUT, not LOST.
  - `test_files_survive_restart`: the watcher succeeds, the engine restarts, then `load` is triggered by a rerun. It still gets `PYSCHED_FILES`.
  - `test_watcher_integration` (real `serve`, real files, `min_age: 1s` and `poll: 1s` given in seconds): the file is written by the test after 1 s, then `load` (`cat "$PYSCHED_FILES"`) succeeds and its stdout log holds the file's content. It finishes within 8 s.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: run file-watcher jobs in the engine`.

### Task 11: Example, README, learning path and release gate

**Files:**
- Create: `examples/alerts.yaml` plus a script if needed. It is the alert routing demo:
  - a `log` notifier `examples/logs/alerts.log`
  - a `command` notifier that echoes the alert kind
  - routes by criticality
  - a job that fails, a job with `max_runtime` that runs long, and a file watcher on `examples/inbox/*.csv`
  - header comments with the exact commands to try it
- Modify: `README.md` (Phase 2 section: alerts, routing, notifiers, watchers; roadmap row v0.2 marked done), `docs/index.md` (learning-path entries for the four new pages), `mkdocs.yml`, and `tests/test_examples.py` (the new example validates)
- Test: `tests/test_examples.py`

- [ ] **Step 1:** Add `test_alerts_example_validates`: `load_config(examples/alerts.yaml)` has no errors. Run it and expect FAIL.
- [ ] **Step 2:** Write the example and the docs, then run it. Expected: PASS.
- [ ] **Step 3: Gate.** Each must be clean:
  - `uv run pytest`, three runs in a row with no flakes
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run pyright src tests`
  - `uv run --with mkdocs-material mkdocs build --strict`
- [ ] **Step 4: Commit** `docs: add the alerts example and phase 2 docs`.

### Task 12: PyStack `schedalerts` command (after the v0.2.0 tag)

Done by the controller after merge and tag, in a PyStack worktree (`../pystack-schedalerts-wt`), following `plugins/sched_plugin.py`:
- bump the `pyscheduler` tag to `v0.2.0` (pymq stays at `v0.3.1`)
- add a `schedalerts` command to `PySchedulerPlugin`, in a new module `plugins/_schedalerts.py`:
  - in a temp folder it writes a config with a `log` notifier, a route `alert.#` and a job that fails (`exit 1`, no retries)
  - it runs `serve` in-process, triggers the job for `2026-10-19` and waits for the alert line, then prints the alert (`kind`, `criticality`, `message`) and its delivery status from `GET /api/alerts`
- add the test `tests/test_schedalerts.py`, which asserts the output contains `failed`, `sent` and the run id. It is deterministic and runs in under 5 s.
- update the README and `docs/index.md` command tables
- PR, CI green, merge
