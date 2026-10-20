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

## Phase 3: The Web UI

A noticeboard in the school hall beats asking the secretary. Phase 3 adds
one, served by the scheduler itself with no extra install and no build
step:

- **Login and roles**: a `users_file` (make a line with `pysched hash-password`)
  gives each person a role: viewer, operator or admin. Bearer tokens and
  browser sessions go through one access layer, with a CSRF token on
  anything a cookie changes.
- **The board** (`/`): a row per run for any date, with state badges,
  duration against the job's typical duration, filters, and critical
  failures pinned to the top. Operators get rerun, hold, release, kill and
  the other actions as buttons.
- **Run and job pages**: attempts, events, alerts and their deliveries, the
  definition the run used, and a **live log tail**.
- **The dependency graph** (`/graph`): layered left to right, coloured by
  state, with failure and cross-date edges drawn differently and a focus
  mode that lights up everything upstream and downstream.
- **The timeline** (`/timeline`): hour, day and week views of when every job
  really ran, with forecast "ghost" bars for what is still to come; and the
  forecast table (`/forecast`).
- **Admin** (`/admin`): config versions with a diff against the one
  before, and the dead-letter tray with a replay button.
- **Live updates**: pages follow the scheduler over server-sent events with
  numbered ids, so a dropped connection catches up (or reloads) by itself.

Try it with the seeded demo: twelve jobs, three days of real runs, a flaky
job, a file watcher and a critical failure. It listens on loopback only.

```bash
uv run python examples/ui_demo.py          # prints the address; log in as admin / demo
uv run python examples/ui_demo.py --seed-only
```

Concept pages: [The Web UI](docs/concepts/web-ui.md),
[Live Updates](docs/concepts/live-updates.md),
[Graph Layout](docs/concepts/graph-layout.md) and
[Timelines](docs/concepts/timelines.md); the auth modes are in
[The HTTP API](docs/concepts/http-api.md).

## Phase 4: Remote Agents

A shop with drivers can deliver across town. Phase 4 lets a job run on
another machine:

- **Agents**: `pysched-agent` is a worker that takes sealed orders from a
  pymq broker, runs them with the same minder the scheduler uses and
  reports back. A job picks its place with `agent: name` or
  `agent_tags: [gpu]`.
- **An `agents:` section**: the scheduler embeds a broker (`listen`) or
  uses an existing one (`broker`), with a secrets file naming each agent.
- **Heartbeats**: an agent says "still here" every 10 seconds. Silence for
  `agent_grace` marks it offline; an attempt unheard for `lost_after` is
  failed as `lost`, and retries apply.
- **Remote logs**: fetched from the agent on demand (503 if it is offline).
- **Signed messages**: every message carries an HMAC seal, a date and a
  one-time nonce. The broker itself has no authentication or encryption,
  so use loopback, a trusted network or a tunnel.
- `GET /api/agents`, the `/agents` page and `pysched agents` show who is online.

Try it: a scheduler and two agents in one process, a pinned job and a
tagged job, and a printout of which agent ran each:

```bash
uv run python examples/agents_demo.py
```

Concept pages: [Remote Agents](docs/concepts/remote-agents.md),
[Heartbeats](docs/concepts/heartbeats.md) and
[Signed Messages](docs/concepts/signed-messages.md).

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
the service and CLI, the web UI, live updates, graph layout, timelines,
remote agents, heartbeats and signed messages.

Build the docs site with:

```bash
uv run --with mkdocs-material mkdocs build --strict
```

## Roadmap

| Release | Contents |
|---------|----------|
| v0.1 | **Core engine**: YAML reader, config and validation, schedules, calendars, time zones, run dates, forecast, journalled store, the minder, local runs, dependencies, retries, pools, holds, catch-up, backfill, rerun, JSON HTTP API, `pysched` CLI |
| v0.2 (done) | **Alerts and file watchers**: alert rules (failed, never started, too long, too short, lost), routing through a pymq broker, notifiers (log, command, webhook, Slack, Telegram, email), file-watcher jobs |
| v0.3 (done) | **Web UI**: login and roles, job board, run detail with live log tail, dependency graph, timeline with forecast, operator actions, live updates over SSE |
| v0.4 (done) | **Remote agents**: `pysched-agent` workers over a pymq broker, heartbeats, lost-agent handling, remote log fetch, signed messages |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

See [CONTRIBUTING.md](CONTRIBUTING.md).
