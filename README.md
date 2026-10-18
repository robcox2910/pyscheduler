# PyScheduler

An educational Autosys / Control-M style job scheduler, built from scratch
in Python on our own libraries: [pynet](https://github.com/robcox2910/pynet)
(event loop and processes), [pyweb](https://github.com/robcox2910/pyweb)
(web server) and [pydb](https://github.com/robcox2910/pydb) (journalled
database). From v0.2, alerts and remote agents also use
[pymq](https://github.com/robcox2910/pymq) (message broker).

## What It Is

A school runs on a timetable: maths at nine, lunch at twelve, and the
science lab only once the kit is out. PyScheduler is that timetable for
computer jobs. You describe your jobs in a YAML file and it:

- fires them on cron-style schedules, in their own time zones, on business
  calendars with holidays, through clock changes;
- waits for dependencies (on success, on failure or on any ending), and
  works out which business day each run is for;
- retries failures with growing waits, times jobs out, and keeps to a
  global cap and named pools;
- runs every attempt under a small **minder** process that writes down how
  it ended, so a crash of the scheduler loses nothing: on restart it
  recovers, adopts what is still running and catches up what it missed;
- saves everything write-ahead to a journalled store, in group commits;
- lets operators hold, release, kill, rerun, force and backfill runs over a
  JSON HTTP API with bearer tokens, or with the `pysched` command line;
- forecasts what will run, and checks a config for every mistake before it
  ever runs.

It is concurrent: one event loop looks after every running job. In a test,
200 real jobs started together and finished in about 3 s; about 35 are
alive at any instant, because each attempt pays a Python start-up for its
minder. Through it all, 99 % of the loop's 10 ms ticks are less than
50 ms late.

## Quick Start

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/). POSIX only.

```bash
uv sync --all-extras
uv run pysched validate examples/etl.yaml       # check the example config
uv run pysched serve examples/etl.yaml          # start the scheduler (leave it running)
```

In another terminal:

```bash
uv run pysched trigger extract --run-date 2026-10-16
uv run pysched trigger fetch-rates --run-date 2026-10-16
uv run pysched status --date 2026-10-16
uv run pysched log fetch-rates.2026-10-16.1 --attempt 1 --stderr
```

`examples/etl.yaml` is a small nightly chain on a UK business calendar:
`extract` then `transform`, and a flaky `fetch-rates` that fails its first
attempt and succeeds on the retry, all feeding `load` (which shares a
one-slot `warehouse` pool with `transform`), plus a `cleanup` job that only
runs on a day `load` fails. Runs use `previous_business_day`, so Monday's
02:00 run processes Friday.

## Phase 2: Alerts and File Watchers

A smoke alarm is no use if nobody hears it. Phase 2 adds the ears:

- **Alerts**: the scheduler raises an alert when a run `failed`, is
  `retrying`, `blocked`, `lost`, ran `too_short` or `too_long`, or
  `never_started` by its `must_start_by`; when a file watcher gives up
  (`watcher_timeout`); and when a config reload fails (`reload_failed`). A job sets `min_runtime`,
  `max_runtime` and `must_start_by`, and its own `alerts:` to switch kinds
  on or off.
- **Routing**: every alert has a topic, `alert.<kind>.<criticality>.<job>`.
  `routes:` match topics with `*` (one word) and `#` (any number of words)
  and pick notifiers. Deliveries are written to the store first, retried
  with backoff, and replayed after a restart.
- **Notifiers**: `log`, `command`, `webhook`, `slack`, `telegram` and
  `email`, with `${SECRET}` values kept out of the file.
- **File watchers**: a job with `type: file_watcher` waits for files
  matching `watch` to arrive and sit still for `min_age`, then succeeds
  and hands the list to its dependents as `{files}` (and
  `PYSCHED_FILES`). It uses no pool or running slot while waiting, and
  raises `watcher_timeout` after `give_up_after`.

Try it with `examples/alerts.yaml`:

```bash
uv run pysched validate examples/alerts.yaml
uv run pysched serve examples/alerts.yaml          # leave it running
```

In another terminal:

```bash
uv run pysched trigger always-fails --run-date 2026-10-16
uv run pysched trigger slow-job --run-date 2026-10-16
touch examples/inbox/trades.csv
uv run pysched trigger inbox --run-date 2026-10-16
uv run pysched alerts
cat examples/logs/alerts.log
```

`pysched alerts` lists what was raised and which deliveries were sent,
and `pysched replay <alert-id>` sends an alert's dead deliveries again. Concept pages:
[Alerts](docs/concepts/alerts.md), [Routing Alerts](docs/concepts/routing-alerts.md),
[Notifiers](docs/concepts/notifiers.md) and [File Watchers](docs/concepts/file-watchers.md).

## Config Reference

The config format (scheduler settings, calendars, pools, defaults and
every job key) is described in the docs:

- [Config Files](docs/concepts/config.md) and [YAML](docs/concepts/yaml.md)
- [Templates and Values](docs/concepts/templates.md) for `{run_date}` and `${SECRET}`
- [Cron Expressions](docs/concepts/cron.md) and [Calendars and Run Dates](docs/concepts/calendars-and-run-dates.md)
- [Validation](docs/concepts/validation.md) for every check `pysched validate` makes

The full design is in `docs/superpowers/specs/2026-10-17-pyscheduler-design.md`.

## Concepts

Every concept has a kid-friendly page with an everyday analogy and a
runnable snippet. Start at [docs/index.md](docs/index.md) for the learning
path:

scheduler, YAML, config files, templates, cron, calendars and run dates,
time zones and DST, dependencies, runs and states, retries and backoff,
pools and limits, the minder, clocks and launchers, group commit,
concurrency, recovery and catch-up, validation, operator actions,
forecast, notifiers, routing alerts, alerts, file watchers, the HTTP API,
and the service and CLI.

Build the docs site with:

```bash
uv run --with mkdocs-material mkdocs build --strict
```

## Roadmap

| Release | Contents |
|---------|----------|
| v0.1 | **Core engine**: YAML reader, config and validation, schedules, calendars, time zones, run dates, forecast, journalled store, the minder, local runs, dependencies, retries, pools, holds, catch-up, backfill, rerun, JSON HTTP API, `pysched` CLI |
| v0.2 (done) | **Alerts and file watchers**: alert rules (failed, never started, too long, too short, lost), routing through a pymq broker, notifiers (log, command, webhook, Slack, Telegram, email), file-watcher jobs |
| v0.3 | **Web UI**: login and roles, job board, run detail with live log tail, dependency graph, timeline with forecast, operator actions, live updates over SSE |
| v0.4 | **Remote agents**: `pysched-agent` workers over a pymq broker, heartbeats, lost-agent handling, remote log fetch, signed messages |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

See [CONTRIBUTING.md](CONTRIBUTING.md).
