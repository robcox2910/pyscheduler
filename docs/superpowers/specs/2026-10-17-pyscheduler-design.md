# PyScheduler — Design

**Date:** 2026-10-17
**Repo:** pyscheduler (new), pystack (integration)
**Built on:** pynet v0.1.0 (event loop, processes, TCP), pymq v0.2.0 (broker),
pyweb v0.2.0 (App, AsyncServer, SSE, auth), pydb v0.2.0 (journalled database).

## Why

The user wants an Autosys / Control-M style job scheduler as the next
learning module, built on our own libraries. It is a long-running service
that runs any configured command, at the right time, in the right order,
and tells people when things go wrong. Efficient concurrency is
fundamental: one scheduler process on PyNet's single-threaded event loop
should comfortably run hundreds of jobs at once without a thread per job.

The user's requirements, from the brainstorm on 2026-10-16:

- runs any configured command; the config is YAML
- dependencies, retries, criticality levels
- stdout/stderr log locations, a "run date" (business date), rerun
- calendars, and schedules at least as flexible as cron
- upfront validation of a bad setup, for example dependencies between jobs
  on different calendars
- alerts when a job fails, never starts, runs too long or runs too short,
  with pluggable notifiers (email, Slack, Telegram)
- file watchers and remote agents
- a web UI with a job board as the home page, a dependency graph, and a
  timeline/Gantt view

The user then handed every further design choice to Claude ("you decide",
"keep going"). Everything below that is not in that list is Claude's call,
including the extras Claude had proposed: dependencies across run dates,
time zones and daylight saving, catch-up and backfill, a forecast,
resource pools, on-failure/on-done dependencies, and hold/force-start.
**All of these are in.**

## Phases (one plan and one release each)

The scheduler is too big for one plan. It ships in four phases. Each phase
is useful on its own, is released as a tag, and is integrated into PyStack
straight away.

| Phase | Release | Contents |
|-------|---------|----------|
| 0. PyDB speed-up | pydb v0.3.0 | Copy-on-first-write transactions, hash-indexed PRIMARY KEY/UNIQUE checks (see The store) |
| 1. Core engine | v0.1.0 | YAML reader, config and validation, schedules, calendars, time zones, run dates, forecast, journalled store, the minder (job wrapper), local runs, dependencies, retries, pools, holds, catch-up, backfill, rerun, JSON HTTP API, `pysched` CLI |
| 2. Alerts and watchers | v0.2.0 | Alert rules (failed, never started, too long, too short, lost), routing through a pymq broker, notifiers (log, command, webhook, Slack, Telegram, email), file-watcher jobs |
| 3. Web UI | v0.3.0 | Login and roles, job board (home), job/run detail with live log tail, dependency graph, timeline/Gantt with forecast, operator actions, live updates over SSE |
| 4. Remote agents | v0.4.0 | `pysched-agent` worker processes reached over a pymq `BrokerServer`, heartbeats, lost-agent handling, remote log fetch, signed messages |

This document fixes the design for all four phases, so later phases do not
force rewrites of earlier ones. Phase 1 is specified in full detail.
Phases 2 to 4 are specified to the level their plans need, and each gets a
short addendum before its plan is written if the build has taught us
something.

## Goals

1. One process, one event loop, many jobs: job processes, timers, file
   polls, HTTP clients and notifier calls all run concurrently on PyNet's
   loop. Blocking work (database commits, SMTP, DNS) goes to worker
   threads with `run_in_thread` and never stalls the loop.
2. Nothing acknowledged is lost: every state change of a run is committed
   to a journalled PyDB before the scheduler acts on it or reports it.
   After a crash or restart, the scheduler knows every run's state,
   including runs whose processes kept going while it was down.
3. A bad configuration is refused with every error listed at once, each
   with the file, line and job it concerns, before anything runs.
4. Schedules can express everything cron can, plus calendar-aware rules
   (business days, nth business day, last day of month), multiple
   schedules per job, intraday windows, and time zones with correct
   daylight-saving behaviour.
5. Kid-friendly docs for every concept, runnable examples, and a PyStack
   demo for every phase.

## Non-goals

- High availability or a cluster of schedulers (one scheduler process per
  state directory; the PyDB lock file enforces it).
- "Box" jobs (Autosys's job containers). Jobs carry `tags` for grouping
  and filtering instead.
- A config editor in the UI. YAML files in version control are the source
  of truth; the UI and API can reload them.
- Windows support (POSIX process groups and signals are used throughout).
- TLS on the scheduler's own HTTP listener (put it behind a reverse proxy;
  documented).

## Anti-goals

- A job that runs twice for the same run because of a crash or a race.
- A job that starts before its dependencies are met.
- A silent failure: a job that fails, is lost, or never starts without
  anyone being told (from phase 2), or without it being visible in the API.
- The event loop blocking on disk, network or a child process.
- A config that half-loads: a reload is all-or-nothing.

## Constraints

- Python ≥ 3.14, uv, ruff (same rule set as the sibling repos), pyright
  strict, pytest with ≥ 80 % coverage and a 10 s per-test timeout,
  conventional commits, PRs into a protected `main`.
- No `TYPE_CHECKING`, no `from __future__ import annotations`.
- Files under about 400 lines.
- Runtime dependencies: only our siblings (pynet, pymq, pyweb, pydb),
  pinned by git tag in `[tool.uv.sources]`, plus the standard library
  (`zoneinfo`, `smtplib`, `urllib`, `hmac`, `glob`). **No PyYAML**: we write
  our own YAML-subset reader (Claude's call: in keeping with the series,
  and it lets us give line numbers in every config error).
- Package `pyscheduler`, CLI `pysched` (phase 4 adds `pysched-agent`).
- POSIX only (macOS and Linux); CI runs on Linux.

## Vocabulary

- **Job:** a named definition from the config: what to run, when, and
  with what rules.
- **Run date:** the business date a run belongs to (`2026-10-17`), which
  is not always the date it happens to execute on.
- **Run:** one instance of a job for one scheduled moment. Identified by
  a run id; it belongs to a job and a run date.
- **Attempt:** one execution of a run's command. Retries and reruns add
  attempts to the same run.
- **Firing:** the moment a schedule says a job is due.

## Phase 1 — Core engine

### Configuration files

`pysched serve scheduler.yaml` loads one main file, which may `include:` a
list of other files or glob patterns (relative to the including file).
Top-level keys:

```yaml
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
```

Job keys (all optional except `command`):

| Key | Meaning |
|-----|---------|
| `command` | A string (run with `/bin/sh -c`) or a list (run directly). Placeholders: `{job}`, `{run_date}` (ISO), `{run_date:%Y%m%d}` (any strftime), `{run_id}`, `{attempt}`, `{scheduled_at}`. Unknown placeholders are a validation error. |
| `cwd`, `env` | Working directory and extra environment variables (placeholders allowed). The scheduler adds `PYSCHED_JOB`, `PYSCHED_RUN_DATE`, `PYSCHED_RUN_ID`, `PYSCHED_ATTEMPT`. The rest of the environment is inherited from the scheduler. |
| `schedule` | One schedule or a list (see Schedules). Absent means the job runs only when its dependencies create a run (a dependency-only job) or by hand. `manual` means by hand only. |
| `calendar` | A calendar name, or `none` (every day is a business day). Default: none. |
| `timezone` | Overrides the scheduler's. |
| `run_date` | `same_day` (default) or `previous_business_day`. |
| `depends_on` | List of dependencies (see Dependencies). |
| `retries` | `{count, delay, backoff, max_delay}`; default `count: 0`. |
| `timeout` | Kill the attempt after this long (counts as a failed attempt; may retry). |
| `criticality` | `low`, `normal` (default), `high`, `critical`. |
| `pools` | Map of pool name → slots needed. |
| `overlap` | What to do if a firing arrives while the previous run of the job is still active: `queue` (default), `skip`, `allow`. |
| `catch_up` | Missed firings while the scheduler was down: `latest` (default), `all`, `none`. |
| `stdout`, `stderr` | Log file templates. Default `{log_dir}/{job}/{run_date}/{run_id}.{attempt}.out` and `.err`. `stderr: stdout` merges the two. |
| `min_runtime`, `max_runtime`, `must_start_by`, `alerts` | Used by phase 2; accepted and validated in phase 1. |
| `agent`, `agent_tags` | Used by phase 4; accepted and validated in phase 1 (a phase 1 scheduler refuses jobs that name an agent). |
| `tags`, `description`, `owner` | Free text for people and the UI. |
| `enabled` | `false` keeps the job in the config but never creates runs for it. |

Durations are written `90s`, `15m`, `2h`, `1h30m`, `1d`. Times are `HH:MM`
in the job's time zone. Secrets and machine-specific values can use
`${ENV_NAME}` anywhere in a string value; a missing variable is a
validation error.

### The YAML reader

`pyscheduler.yamlish` reads the subset of YAML that config files need and
refuses the rest with a clear message and a line number:

- block mappings and block sequences (indentation with spaces; tabs are an
  error), flow sequences `[a, b]` and flow mappings `{a: 1, b: x}`
- plain, single-quoted and double-quoted scalars (double quotes support
  `\n`, `\t`, `\"`, `\\`, `\uXXXX`), `|` and `>` block scalars (with
  `-`/`+` chomping), `#` comments, a leading `---`
- duplicate keys are an error
- anchors, aliases, tags, multiple documents and complex keys are refused

Every scalar comes back as a **string** together with its line number;
the config layer converts each field to the type that field expects (so
`no` is never silently turned into `False`, and `08:00` stays a time).
The result is a tree of nodes carrying line numbers, so validation errors
can say `jobs.yaml:42: job load-warehouse: …`.

### Schedules

A schedule is one of:

1. A cron expression with five fields (minute, hour, day of month, month,
   day of week), with lists, ranges, steps and names (`jan`, `mon`), and
   the macros `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`. Extras
   from Quartz-style cron: `L` (last day of month) in day-of-month, and
   `mon#2` (second Monday) and `friL` (last Friday) in day-of-week. When
   both day of month and day of week are restricted, either may match
   (standard cron OR rule; documented, with an example).
2. A mapping for things cron can't say neatly:
   - `{every: 15m, between: "08:00-18:00"}`: repeat within a daily window
     (the window may cross midnight, e.g. `"22:00-02:00"`).
   - `{at: ["09:00", "17:30"]}`: listed times every day.
   - `{business_day: 1, at: "07:00"}` (first business day of the month) or
     `{business_day: -1, at: "18:00"}` (last). Needs a calendar.
   - Any mapping may add `days: [mon, wed]` and `months: [1, 4, 7, 10]`.
3. `manual`.

A job may list several schedules; its firings are the union (two
schedules firing at the same instant produce one firing).

**Calendars filter firings:** a firing whose local landing day is not a
business day of the job's calendar is dropped. This holds for both run-date
rules, so a `0 2 * * *` job on a Monday-to-Friday calendar with
`run_date: previous_business_day` fires Tuesday to Friday (for Monday to
Thursday) and on Monday (for Friday): exactly one firing per run date.

**Time zones and daylight saving:** schedules are read in the job's time
zone (`zoneinfo`). A local time that does not exist (the spring-forward
gap) fires at the first instant after the gap. A local time that happens
twice (the autumn fall-back) fires once, at the first occurrence.
Intraday `every` schedules step in real elapsed time inside the window,
so a 15-minute schedule has no extra or missing firings across a change.
All instants are stored as UTC `TIMESTAMP`s.

**Run dates:** a firing's run date is its local calendar date in the
job's time zone (`same_day`). With `previous_business_day`, it is the
business day before that date according to the job's calendar (useful for
a 02:00 job processing yesterday's trades). A dependency-only job takes the
run date of the upstream run that created it.

The schedule module exposes pure functions, with no clock and no loop, so
they are easy to test:

- `next_firing(job, after: datetime) -> Firing | None`, where a `Firing`
  is `(scheduled_at: datetime (UTC), run_date: date)`
- `firings_between(job, start, end) -> Iterator[Firing]`
- `forecast(config, start, end) -> list[ForecastEntry]`: scheduled jobs'
  firings, plus their dependency-only downstream jobs on the same run
  dates, with an expected start (latest of the firing and the upstream
  expected end, using the median of the last 10 successful durations,
  or none) and expected duration. Used by the API now and by the timeline
  in phase 3.

A schedule that can never fire (for example `0 0 30 2 *`) is a
validation error (checked over the next 4 years of firings).

### Dependencies

`depends_on` is a list; each item is a job name (shorthand for
`{job: name}`) or a mapping:

```yaml
depends_on:
  - extract-trades                         # success, same run date
  - {job: reconcile, on: done}             # success OR failure
  - {job: load-warehouse, on: failure}     # run only if that failed
  - {job: close-of-day, run_date: -1}      # previous business day's run
```

- `on`: `success` (default), `failure`, `done` (any final state:
  succeeded, failed or skipped).
- `run_date`: an offset in business days of the downstream job's
  calendar (`0` default, `-1` previous). Only zero or negative offsets.
- A dependency is judged on the upstream's **latest** run for the target
  run date (by `scheduled_at`). If that run is not final, the downstream
  keeps waiting. If the upstream has no run for that date yet, the
  downstream waits.
- A dependency with `on: success` whose upstream run ends failed (after
  all retries) makes the downstream run **blocked**, with the reason. It is
  not failed: it starts if the upstream is later rerun and succeeds, or if
  an operator force-starts it or skips the upstream.
- A dependency with `on: failure` whose upstream succeeded makes the
  downstream run end as **skipped** (with the reason).
- **Dependency-only jobs** (no `schedule`) get exactly one run per run
  date, created when the first run of any upstream job is created for that
  run date (with the run-date offset applied in reverse). Its
  `scheduled_at` is the creating upstream run's `scheduled_at`.
- **Uniqueness:** a scheduled run (schedule, catch-up, backfill) is
  unique by `(job, scheduled_at)`, and a dependency-only run by
  `(job, run_date)`. Manual triggers always create a new run.

### Validation

`pysched validate scheduler.yaml` and every load or reload run the same
checks and report **all** problems, each with file, line and job. Errors
stop the load. Warnings are printed and returned by the API, but the load
continues.

Errors:

- names (jobs, calendars, pools, notifiers, agents) that are not
  1–64 characters of `[A-Za-z0-9_-]` (they appear in run ids, log paths and
  pymq topic words, which allow only that set)
- YAML syntax and refused YAML features; unknown keys (with a "did you
  mean" suggestion); wrong value types; bad durations, times or dates;
  missing `${ENV}` variables; duplicate job names across included files
- a dependency on an unknown job; a dependency cycle (counting only
  dependencies with run-date offset 0); a positive run-date offset
- **a dependency between jobs on different calendars** (the user's
  example), or between a calendar job and a `none`-calendar job. The fix
  is to put both on one calendar.
- bad cron or schedule mappings; `business_day` without a calendar; a
  schedule that never fires; an unknown time zone
- an unknown calendar or pool; a job needing more slots than a pool has;
  a calendar `include` cycle
- unknown placeholders in `command`, `cwd`, `env`, `stdout`, `stderr`
- `min_runtime >= max_runtime`; `max_runtime` greater than `timeout`;
  negative retry counts; `backoff < 1`
- a job naming an `agent` (phase 1 only)
- a job with neither `schedule` nor `depends_on` and not `manual`

Warnings:

- a scheduled downstream whose schedule (within the next 366 days) fires
  on run dates where an upstream has no firing and no way to get a run, so
  it would wait forever
- depending on a job that fires more than once per run date (only its
  latest run counts)
- `overlap: allow` together with `pools` (it may hog the pool)

### The run lifecycle

Run states:

```
scheduled ──> waiting ──> queued ──> running ──> succeeded
                │  ▲         ▲          │   └──> retry_wait ──> queued
                │  │         │          └──────> failed
                ▼  │         │
              blocked       held  (from waiting / queued / retry_wait)
                         skipped, cancelled  (operator, or on: failure rules)
```

- **scheduled:** created at its firing (or by a dependency, a backfill or
  a manual trigger).
- **waiting:** waiting for dependencies, or for the previous run of the
  same job (`overlap: queue`). For `overlap`, a job's run is **active**
  only while it is `queued` or `running` (it holds or wants a process
  slot). Waiting, blocked, held and retry_wait runs of other run dates do
  not hold back a new run, so one blocked Monday never stalls Tuesday.
- **blocked:** a `success` dependency ended failed (see above).
- **queued:** ready to start, waiting for a free slot in the global cap,
  the job's pools, or both. Queued runs start in order of criticality
  (critical first), then `scheduled_at`, then run id. A queued run that
  needs a busy pool does not hold back runs that need other pools.
- **running:** an attempt is executing.
- **retry_wait:** an attempt failed and retries remain; the run waits for
  `delay × backoff^(n-1)`, capped by `max_delay`, then queues again.
- **succeeded / failed / skipped / cancelled:** final.
- **held:** an operator froze it. Release returns it to where it would
  otherwise be.

An attempt fails on a non-zero exit, a kill signal, a timeout, a command
that cannot be started (for example a missing program), or being **lost**.
"Lost" means the scheduler cannot find out how the attempt ended after a
restart (see Recovery). A kill by an operator ends the run as failed with
reason `killed`, and does not retry.

Operator actions, available through the API and CLI:

- `trigger JOB [--run-date D]`: create a manual run now.
- `rerun RUN [--downstream]`: reopen a final run. It adds a new attempt
  with the retry budget reset. With `--downstream`, it also reopens the
  final runs of jobs that depend on it for the same run date, transitively,
  so they wait again.
- `hold RUN` / `release RUN`
- `kill RUN`: SIGTERM to the process group, SIGKILL after `kill_grace`.
- `force-start RUN`: start now, ignoring dependencies and holds, but not
  pools or the global cap.
- `skip RUN`: end as skipped. `on: success` downstreams treat a skipped
  upstream as satisfied (documented: "skip means pretend it worked"), and
  `on: failure` downstreams treat it as not failed.
- `mark-success RUN` / `mark-failed RUN`: set the final state by hand
  (recorded as an operator action).
- `backfill JOB --from D1 --to D2`: create one run for each run date in
  the range (business days of the job's calendar) on which the job has
  no run yet. Its `scheduled_at` is the job's **first** firing for that run
  date (for an intraday job, only that first one is backfilled). For a
  dependency-only job, it is the start of the run date in the job's time
  zone. Backfilled runs create dependency-only downstream runs exactly
  as any other run does.
- `reload`: re-read and validate the config. If it is valid, swap it in.
  If not, keep the old config and return the errors. After a swap:
  - runs with an attempt **running** keep the definition they started
    with until that attempt ends
  - every other open run (scheduled, waiting, blocked, queued, held,
    retry_wait) **adopts the new definition**, and its dependencies are
    re-evaluated at once
  - open, not-running runs of a job that was removed or disabled end as
    `cancelled` (reason `removed by reload` / `disabled`). Running ones
    finish their attempt and do not retry.
  - `config_version` on a run is the version its current or last attempt
    used.

  Reload only checks the config, never the runs, so it cannot fail
  because of existing runs.

Every action is recorded in an `events` table with who did it (API token
name or user), when, and the before/after state.

### Concurrency design (the heart of the engine)

- **One event loop** (`pynet.loop.run`). The engine is a set of tasks:
  - the **clock task**, which sleeps until the next firing across all
    jobs, kept in a timer heap keyed by UTC instant. It wakes at least
    every 60 s and re-checks the wall clock, so clock jumps and sleep or
    suspend are noticed.
  - one **attempt task** per running attempt
  - the **store writer** (below)
  - the **HTTP server** (pyweb `AsyncServer`)
- **State lives in memory, durably mirrored.** The engine holds every
  non-final run, plus the final runs of the last `memory_days` (default 7)
  run dates, in memory, indexed by job and run
  date. Dependency checks are dictionary lookups, not database queries.
  When a run changes state, the engine re-evaluates only the runs that
  depend on that job (a reverse-dependency index), not every run.
- **Write-ahead, then act.** Every state change is first sent to the store.
  The engine awaits its durable commit before it spawns a process, reports
  a change over the API, or acts on a final state. So after a crash, the
  database never shows a run as not started when it was started.
- **Group commit.** The store writer is one task that takes every queued
  change, writes them in one PyDB `Transaction` (one journal entry, one
  `fsync`) on a worker thread through `run_in_thread`, then wakes all the
  waiting callers. Under load, many state changes share one `fsync`. When
  idle, a change commits on its own straight away. The loop never blocks
  on disk.
- **Processes through the minder.** Each attempt runs as
  `python -m pyscheduler.minder --status <file> --stdout <path> --stderr
  <path> -- <argv>`, started with `pynet.process.create_process`
  (`new_session=True`). The minder:
  - opens the log files (creating directories, appending)
  - runs the command as its child, in the same process group
  - forwards SIGTERM to the group
  - writes `{"exit": code, "signal": n, "started_at", "ended_at"}`
    atomically to the status file when the command ends

  The scheduler awaits `Process.wait()` (the SIGCHLD watcher, no polling),
  then reads the status file. Why the minder (Claude's call): it lets the
  scheduler learn how an attempt ended **even if the scheduler itself was
  down at the time**. It also keeps log writing out of the scheduler's
  loop, and phase 4's agent reuses it. The cost is one small extra Python
  process per running attempt.
- **Timeouts** are loop timers on the attempt task (`pynet.loop.timeout`).
  On expiry: SIGTERM to the group, then SIGKILL after `kill_grace`.
- **Admission control:** a counter for the global cap, one counter per
  pool, and a priority queue of queued runs. Each time a slot frees, the
  engine starts every queued run that now fits, in priority order. Waiting
  runs never hold a slot.
- **Back-pressure on firings:** with `overlap: queue`, a job keeps at most
  **one** run per run date waiting on overlap behind an active run. When a
  newer firing for the same run date arrives, the older waiting one ends
  `cancelled` with reason `superseded` and the newest is kept (Claude's
  call: a stuck 15-minute job can't build a queue of hundreds of runs, and
  the freshest run is the useful one). Runs of different run dates queue in
  run-date order and are never superseded. `catch_up: all` and backfill
  follow the same rule. With `overlap: skip`, a firing that arrives while a
  run is active is recorded as `cancelled` (`skipped: overlap`).

### The store (PyDB, `journal=True`)

Tables (DATE/TIMESTAMP columns are PyDB's UTC types):

- `runs`: run_id (TEXT, `<job>.<run_date>.<seq>`, e.g.
  `extract-trades.2026-10-17.1`: no slashes, so it is one URL path
  segment, and job names cannot contain dots), job, run_date DATE,
  scheduled_at TIMESTAMP, state, reason, attempt INT, rerun INT,
  trigger (`schedule`/`dependency`/`manual`/`backfill`/`catch_up`),
  config_version INT, created_at, updated_at
- `attempts`: run_id, attempt, started_at, ended_at, exit_code, signal,
  outcome (`succeeded`/`failed`/`timeout`/`killed`/`lost`/`start_error`),
  pid, stdout_path, stderr_path, status_path
- `events`: id, at, actor, run_id (nullable), action, detail (JSON text)
- `configs`: version INT, loaded_at, digest, source (the file texts, so
  a run can always show the job definition it used)
- `engine`: key/value, including `last_tick` (the last instant up to which
  firings were processed, for catch-up)

The store module hides PyDB behind a small typed API (`StoreWriter.submit`,
`load_open_runs`, `history(job, limit)`, and so on). PyDB's `Transaction`
and `checkpoint()` are used directly; the scheduler checkpoints when it
shuts down cleanly and leaves automatic checkpoints to PyDB.

**History and retention:** final runs, with their attempts and events,
are deleted once their run date is older than `retention` (scheduler
key, default `90d`, checked once an hour, in batches). Log files are left
alone; cleaning them is the operator's job (documented). `history(job,
limit)` and the forecast's "last 10 successful durations" read the store on
a worker thread; the engine keeps a small per-job cache of recent
durations, updated as runs finish. Rerunning a run that has been pruned is
impossible; trigger a new run instead.

**PyDB prerequisite (a small pydb v0.3.0 release, done first):** today a
PyDB `Transaction` copies every row of every table when it begins, and
PRIMARY KEY / UNIQUE checks scan every row. Group commit on a growing
history would get slower with every commit. We own PyDB, so before phase 1
it gains:

- **copy-on-first-write** transactions: a table is copied for rollback
  only when the transaction first changes it
- **hash-indexed PRIMARY KEY / UNIQUE** checks, so checking an insert or
  update costs O(1), not O(rows)

Behaviour and APIs are unchanged; it is a pure performance change, with
a benchmark test. The scheduler pins pydb v0.3.0.

### Recovery and catch-up

On start the scheduler:

1. opens the store, which replays the PyDB journal
2. loads the open runs
3. for each run that was `running`:
   - if the minder's status file exists, finish the attempt from it
   - else, if the minder's process is still alive (pid recorded and
     `os.kill(pid, 0)` succeeds with a matching start-time check through
     the status file's sibling `.pid` file), poll every 2 s until the
     status file appears; it cannot be awaited because it is no longer our
     child
   - else, mark the attempt `lost`, which may retry
4. processes missed firings between `last_tick` and now for each job,
   following its `catch_up` policy. These are created with
   `trigger: catch_up`. A firing whose run already exists is never created
   twice: runs are unique by `(job, scheduled_at)`, enforced by an
   in-memory index rebuilt on load.

### HTTP API and CLI

pyweb `App` on `AsyncServer`, JSON only in phase 1. Auth: bearer tokens
listed in `api_tokens_file`, each `{name, token_sha256, role}` with role
`viewer` or `operator`. Reads need `viewer`, actions need `operator`. When
no tokens file is configured, the API listens on loopback only and allows
everything (documented as development mode).

- `GET /api/health`
- `GET /api/jobs`; `GET /api/jobs/{name}` (definition, next firings,
  recent runs)
- `GET /api/runs?run_date=&job=&state=&limit=`; `GET /api/runs/{id}`
  (with attempts and events)
- `GET /api/runs/{id}/log?stream=stdout&tail=200`
- `GET /api/forecast?from=&to=`
- `POST /api/jobs/{name}/trigger`; `POST /api/jobs/{name}/backfill`
- `POST /api/runs/{id}/{rerun|hold|release|kill|force-start|skip|mark-success|mark-failed}`
- `POST /api/reload`; `GET /api/config/validate` (validate the files on
  disk without loading them)

Run ids are a single path segment (`/api/runs/extract-trades.2026-10-17.1/rerun`).

Auth middleware: phase 1 has its own small middleware that hashes the
bearer token, looks it up in the tokens file, and sets `request.user`
with that role (pyweb's `SessionAuth` bearer tokens are signed session
tokens, not static ones). Phase 3 extends pyweb's `SessionAuth` with a
pluggable bearer-token verifier, so one middleware handles both.

`pysched` CLI (stdlib `argparse` and `urllib`, no event loop needed on
the client side):

- `validate FILE`
- `serve FILE`
- `forecast FILE --from --to`: offline, from the config
- `status [--date]`, `runs`, `show RUN`, `log RUN [--stderr] [--tail N]`
- `trigger`, `rerun`, `hold`, `release`, `kill`, `force-start`, `skip`,
  `mark-success`, `mark-failed`, `backfill`, `reload`
- `--url` and `--token` options, or the `PYSCHED_URL` and `PYSCHED_TOKEN`
  environment variables

Exit codes: 0 ok, 1 error from the server, 2 usage or validation error.

### Logging

The scheduler logs its own events with the stdlib `logging` module, one
line per event with the run id, to stderr and optionally to a file. Job
output never goes through the scheduler's logs.

## Phase 2 — Alerts and file watchers

- **Alert kinds:**
  - `failed`: the run ended failed after all retries
  - `retrying`: off unless enabled
  - `never_started`: still not running at `must_start_by` (a local time on
    the run date, or a duration after `scheduled_at`)
  - `too_long`: still running past `max_runtime`; raised once, while it is
    running
  - `too_short`: succeeded in under `min_runtime`
  - `lost`
  - `blocked`
  - `reload_failed`
  - `watcher_timeout`
- **Rules:**
  - Alert deadlines are loop timers, set when a run is created or
    started and cancelled when it no longer applies. There is no scanning.
  - Each `(run, kind, attempt)` alerts at most once (dedupe).
  - Alerts are stored in an `alerts` table with their delivery status.
- **Routing:**
  - Alerts are published to an in-process pymq `Broker` on topics
    `alert.<kind>.<criticality>.<job>`.
  - Each notifier has its own queue, bound with patterns from the config:
    ```yaml
    notifiers:
      ops-slack: {type: slack, webhook_url: "${SLACK_URL}"}
    routes:
      - {match: "alert.*.critical.#", notify: [ops-slack, oncall-email]}
      - {match: "alert.failed.#", notify: [team-log]}
    ```
  - A worker task per notifier consumes with leases. Failed deliveries
    retry with backoff through pymq's retry/delay support and dead-letter
    after N tries; dead letters show in the API (and in the UI from
    phase 3).
  - Jobs may add `alerts:` overrides (extra routes, or turn kinds off).
  - On restart, alerts whose delivery status is pending are published
    again.
- **Notifiers**, all behind one `Notifier` protocol (`async send(alert)`):
  - `log`: append to a file
  - `command`: run a program with the alert as JSON on stdin, through the
    same process machinery
  - `webhook`: generic JSON POST
  - `slack`: incoming webhook
  - `telegram`: Bot API `sendMessage`
  - `email`: SMTP with STARTTLS, using `smtplib` in a worker thread

  HTTP-based notifiers use stdlib `urllib` in `run_in_thread`, with a
  timeout. New notifier types register through an entry point
  (`pyscheduler.notifiers`).
- **File watchers** are a job type (the Autosys way), so downstreams use
  ordinary dependencies:
  ```yaml
  trades-file:
    type: file_watcher
    watch: /data/in/trades_{run_date:%Y%m%d}.csv   # glob allowed
    min_age: 30s           # unchanged size and mtime for this long = arrived
    poll: 10s
    give_up_after: 4h      # then fail (alert watcher_timeout)
    schedule: "0 6 * * mon-fri"
    calendar: uk-business
  ```
  - A watcher run is "running" while it polls; it succeeds when a match is
    stable.
  - Downstream commands get the matched paths in `PYSCHED_FILES`, and in
    `{files}` for the watcher's direct dependants.
  - Polling `stat`s the files in a worker thread, with a configurable
    shared limit on concurrent polls.
  - Watchers take no global-cap slot (Claude's call: they don't use a
    process).

## Phase 3 — Web UI

- **Server and pages:**
  - Server-rendered HTML with pyweb's escaping templates, a little
    vanilla JavaScript, and no frontend framework or build step.
  - Static assets come through `mount_static`.
  - Live updates use one SSE stream (`/events`) fed by an `EventHub` that
    the engine publishes run changes to. Pages patch rows in place, and
    fall back to a full reload after a gap (SSE `id` plus a replay buffer).
- **Auth:**
  - pyweb `SessionAuth` with users in a `users.yaml` (password hashes from
    `pysched hash-password`) and roles `viewer`, `operator`, `admin`.
    CSRF on every form or POST.
  - API bearer tokens keep working.
- **Job board (home, `/`):**
  - one row per run for the selected run date (default: today in the
    scheduler's time zone), with the job, state badge, criticality,
    scheduled, start, end, duration vs typical, attempt and tags
  - filters for state, tag, criticality and free text
  - counts by state at the top
  - date picker and previous/next day
  - operator actions per row, with a confirmation step
  - critical failures pinned to the top
- **Job detail and run detail:**
  - the definition (from the config version the run used), next firings,
    history with a duration sparkline, attempts and events
  - log viewer with live tail over SSE, reading the log file's new bytes
    on a timer in a worker thread
- **Dependency graph (`/graph?run_date=`):**
  - computed on the server as a layered layout: longest-path layers,
    barycentre ordering in a few sweeps, and edges as SVG paths
  - nodes coloured by that run date's state; edge style per `on` kind;
    cross-date edges dashed
  - click to open the run; filter by tag; highlight the upstream and
    downstream of a selected job
- **Timeline / Gantt (`/timeline?from=&to=`):**
  - SVG bars per job lane for actual attempts, coloured by outcome, with
    retries as segments
  - ghost bars for forecast runs, a "now" line, and zoom between hour, day
    and week
- **Forecast (`/forecast`):** a table of upcoming runs and their expected
  times.
- **Admin:**
  - config versions with a diff of the source texts, and a reload button
    that shows validation errors
  - notifier dead letters, with replay

## Phase 4 — Remote agents

- **Broker and agents:**
  - The scheduler embeds a pymq `BrokerServer` (or connects to an
    external one; config `agents.broker`).
  - Agents run `pysched-agent --broker host:port --name build-01 --tags
    linux,gpu --secret-file …` and connect with `BrokerClient`, which
    reconnects on its own.
- **Dispatch:**
  - A job with `agent: build-01`, or `agent_tags: [gpu]` (any connected
    agent with all the tags, least busy first), is dispatched as a work
    message on queue `agent.<name>.work`.
  - The agent runs it through the same minder, with its own log
    directory, and reports `started`, `heartbeat` (every 10 s) and
    `finished` on queue `scheduler.reports`.
  - Messages carry an HMAC-SHA256 signature with the agent's shared
    secret, plus a timestamp and nonce, and the scheduler rejects unsigned
    or stale ones. The agent rejects unsigned work.
  - The broker binds to loopback unless configured otherwise. Plain TCP is
    documented as needing a trusted network or a tunnel.
- **Durability:**
  - The agent keeps undelivered `finished` reports in its own small
    journalled PyDB until the scheduler acks them, so an agent that loses
    its connection still reports the outcome later.
  - Dispatch is idempotent by `(run_id, attempt)`.
- **Liveness:**
  - An agent missing heartbeats for `agent_grace` (default 60 s) is
    marked offline.
  - Its running attempts become `lost` after a further `lost_after`
    (default 10 m), unless it reconnects and reports. Runs targeting only
    offline agents wait in `queued` with the reason.
- **Admission:** each agent advertises `max_running`, and global caps and
  pools still apply.
- **Logs:** log tail and fetch use request/reply over the broker.
- **UI:** an agents page showing each agent's status, tags, load and last
  heartbeat.

## Docs (kid-friendly, every concept)

`docs/concepts/` pages, written as each phase lands. Each page uses an
everyday analogy and has a runnable snippet.

- **Phase 1:**
  - `scheduler` (the school timetable)
  - `yaml` (a recipe card with indents)
  - `cron` (the alarm clock with many dials)
  - `calendars-and-run-dates` (school days, and "which day's homework is
    this?")
  - `time-zones-and-dst` (the clocks change, and the alarm still rings
    once)
  - `dependencies` (you can't ice the cake before it's baked)
  - `retries-and-backoff`
  - `pools-and-limits` (only two kids on the trampoline)
  - `runs-and-states` (the traffic lights of a run)
  - `the-minder` (a babysitter who writes a note when the job's done)
  - `group-commit` (one trip to the post box for many letters)
  - `recovery-and-catch-up`
  - `validation` (the teacher who checks your work before the test)
  - `operator-actions`
  - `forecast`
- **Phase 2:** `alerts`, `routing-alerts`, `notifiers`, `file-watchers`.
- **Phase 3:** `web-ui`, `live-updates`, `graph-layout`, `timelines`.
- **Phase 4:** `remote-agents`, `heartbeats`, `signed-messages`.

Each phase also adds `docs/index.md` learning-path entries, a README
section, and examples:

- phase 1: `examples/etl.yaml` with scripts that sleep and fail sometimes
- phase 2: an alert routing demo
- phase 3: a seeded UI demo
- phase 4: a two-agent demo on localhost

## PyStack integration

- **Phase 1:** a `sched` command, deterministic and under 5 s. In a temp
  directory it:
  - writes a small config: three jobs in a chain plus one on-failure
    cleanup job, short `sleep`/`exit` commands, one job that fails once
    then succeeds on retry
  - starts the engine in-process on the PyNet loop with its HTTP API
  - triggers the chain for a fixed run date and waits for it to finish
  - prints the board (state, attempts, durations), then restarts the
    engine on the same state directory to show that history survived
- **Phase 2:** `schedalerts` (a failing job, alert routed to a log
  notifier, printed).
- **Phase 3:** `schedweb` (serves the UI over seeded state until Ctrl-C).
- **Phase 4:** `schedagents`.

PyStack pins each pyscheduler release tag.

## Testing approach

- **Pure modules:** table-driven tests for the YAML reader, cron,
  schedule mappings, calendars, run dates, time zones and DST, and the
  validation rules.
  - Golden firings for known tricky cases: DST gaps and overlaps in
    Europe/London and America/New_York, `L`, `#`, `friL`, the OR rule,
    windows across midnight, `previous_business_day` over a holiday
    weekend.
  - Property test (stdlib `random`, fixed seeds): `next_firing` chained
    from `t` equals `firings_between(t, t+N)`, and firings are strictly
    increasing.
- **Engine:**
  - tests use a fake wall clock and a fake process launcher, so
    dependency, retry, pool, overlap, hold, rerun and catch-up logic run
    without real processes or real time
  - a smaller set of integration tests runs real `sh -c` commands through
    the minder
- **Concurrency:** 200 jobs at once with real `sleep 0.2` processes finish
  in well under the per-test timeout. The engine never runs more than the
  caps (checked through counters), and the loop's longest blocking
  callback stays under 50 ms (a loop-lag probe).
- **Crash tests** use real child processes:
  - the scheduler is killed with SIGKILL while jobs run
  - it is restarted, and the runs finish from the minder status files
  - a minder killed too gives `lost`, which retries
  - catch-up after downtime follows each policy
- **Store:** group commit batches under load (one fsync for many changes,
  observed through a counter); a change is never visible through the API
  before it is durable.
- **API and CLI:** pyweb's `TestClient` covers every endpoint and auth
  role; the CLI is tested against a live in-process server.

## Left to the builder

The internal module split, exact log and error wording, the plan's task
boundaries, the in-memory index data structures, and the exact field
lists of internal dataclasses.
