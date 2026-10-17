# Validation (The Teacher Who Checks Your Work Before the Test)

## Marking Before the Big Day

Imagine handing your homework to a kind teacher the day *before* the test.
They don't just tick the spelling. They read the whole thing and say:
"you used a word that doesn't exist here, these two answers each say
*see the other one*, and this sum can never come out." Better still, they
write **every** mistake on one sheet, so you fix them all in one go.

The [config loader](config.md) checks each box of the form on its own.
**Validation** is the teacher: it looks at the whole config at once,
before a single job runs. Every problem says the file, the line and the
job it belongs to.

```python
import tempfile
from pathlib import Path

from pyscheduler.config import ConfigError, load_config

text = """
calendars:
  uk-business: {weekdays: [mon, tue, wed, thu, fri]}
  us-business: {weekdays: [mon, tue, wed, thu, fri]}
jobs:
  london-report:
    command: make-report --day {rundate}
    schedule: "0 9 * * *"
    calendar: uk-business
    depends_on: [new-york-prices]
  new-york-prices:
    command: fetch-prices
    schedule: "0 0 30 2 *"
    calendar: us-business
"""
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "scheduler.yaml"
    path.write_text(text)
    try:
        load_config(path, env={})
    except ConfigError as exc:
        for problem in exc.problems:
            print(problem.line, problem.job, problem.message)
```

The output lists all three problems together:

```text
6 london-report unknown placeholder {rundate} in command: use one of {attempt}, {job}, {log_dir}, {run_date}, {run_id}, {scheduled_at}
10 london-report different calendars: this job is on 'uk-business' but 'new-york-prices' is on 'us-business'; put both jobs on one calendar
11 new-york-prices schedule never fires (checked over the next 4 years)
```

## Errors: These Stop the Load

- **Dependencies:** a job that doesn't exist (with a "did you mean"
  hint); a **cycle** where jobs wait for each other on the same run date,
  shown as a path like `a -> b -> a` (a `run_date: -1` dependency breaks
  the loop, because yesterday's run is already done); a positive run-date
  offset (you can't wait for tomorrow).
- **Calendars:** two jobs linked by a dependency must share one calendar.
  A UK job waiting on a US job would get stuck on every day that is a
  holiday in one country but not the other.
- **Schedules:** `business_day` without a calendar or beyond 31; a schedule
  that never fires in the next four years (like 30 February); a calendar
  with no business days at all; an unknown time zone.
- **Resources:** an unknown calendar or pool; a job needing more pool
  slots than the pool has; calendars that `include` each other in a loop.
- **Placeholders:** anything in `command`, `cwd`, `env`, `stdout` or
  `stderr` that isn't one of `{job}`, `{run_date}`, `{run_id}`,
  `{attempt}`, `{scheduled_at}` or `{log_dir}`.
- **Settings:** `min_runtime` not less than `max_runtime`; `max_runtime`
  longer than `timeout`; a negative retry count; `backoff` below 1; a job
  naming an `agent` (agents arrive in phase 4); a job with no schedule, no
  dependencies and not `manual`, which could never run.

## Borrowed Calendars

A dependency-only job (no `schedule`) is like a younger sibling who goes
to school whenever the older ones do. If it names no `calendar:`, it
**inherits** the calendar of the jobs it depends on, all the way up the
chain. So in this config `load-warehouse` is on `uk-business` too, and
`config.calendar_for(...)` says so:

```yaml
jobs:
  extract-trades:
    command: extract.sh
    schedule: "30 18 * * *"
    calendar: uk-business
  load-warehouse:              # no calendar: inherits uk-business
    command: load.sh
    depends_on: [extract-trades]
```

If its upstreams are on different calendars there is nothing sensible to
borrow, which is an error: `upstreams are on different calendars
(a: uk-business, b: us-business)`. An upstream with no calendar counts as
the calendar `none`. A scheduled or manual job with no calendar never
inherits; it stays on `none`.

## Warnings: These Are Kept, and the Load Carries On

Warnings end up on `config.warnings`:

- **Would wait forever:** a scheduled job that, some time in the next 366
  days, needs an upstream run that will never exist. For example a daily
  job waiting on a Monday-to-Friday job: on Saturday there is nothing to
  wait for. Dependency-only upstreams are followed back to the jobs that
  feed them.
- **Fires more than once per run date:** only the upstream's latest run
  for that date counts, which may surprise you.
- **`overlap: allow` with pools:** many copies of the job may pile up and
  hog the pool.
- **Depends on a disabled job:** it will never get a run while disabled.

## Not Saying the Same Thing Twice

If the loader already found a mistake in a job (a bad cron string, say),
validation leaves that job alone, so you don't get a second, confusing
"this job has no schedule" message for the same typo. The same goes for a
job name used twice: until you rename one of them, that job's whole-config
checks are skipped, so fix the duplicate and validate again.
