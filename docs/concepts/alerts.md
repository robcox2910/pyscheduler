# Alerts (The Smoke Alarm and the Teacher Who Notices You Never Arrived)

## Two Ways to Notice Trouble

A **smoke alarm** goes off the moment something happens: smoke, beep. A
good **teacher** notices something that did *not* happen: at nine o'clock
she looks at the register, sees your name has no tick, and phones home.

The scheduler's **alert monitor** does both. It listens to the engine,
which tells it every time a run changes state and every time an attempt
starts or ends, and it keeps a few timers for the things that *should*
have happened by now. When something needs a human it raises an
**alert**, which the dispatcher then carries to the notifiers (see
[Routing Alerts](routing-alerts.md)).

## The Kinds of Alert

| Kind | When it is raised |
| --- | --- |
| `failed` | A run ends `failed` (after its retries). |
| `retrying` | A run fails an attempt and waits to try again. Off unless a job turns it on. |
| `blocked` | A run cannot start because an upstream it depends on failed. |
| `lost` | An attempt's process was lost: after a restart nobody can tell how it ended. |
| `too_short` | An attempt *succeeded* in less than the job's `min_runtime`. |
| `too_long` | An attempt is still running when its `max_runtime` is up. |
| `never_started` | A run has not started by its `must_start_by` deadline. |
| `reload_failed` | A config reload had errors, so nothing changed (no job; criticality high). |
| `watcher_timeout` | A file watcher gave up waiting for its file. |

The first four are smoke alarms: they ring when the engine reports the
change. `too_short` is checked when the attempt ends. `too_long` and
`never_started` are the teacher's register: timers.

## The Three Clocks a Job Can Set

```yaml
jobs:
  nightly-report:
    command: ["python", "report.py"]
    schedule: "0 2 * * *"
    run_date: previous_business_day
    min_runtime: 2m       # quicker than this is suspicious
    max_runtime: 1h       # longer than this needs a look
    must_start_by: "03:00"
```

- **`max_runtime`**: a timer starts with each attempt and rings at
  `started_at + max_runtime` if the attempt is still running. It is only
  an alert; the job keeps running (use `timeout:` to stop it).
- **`min_runtime`**: when an attempt *succeeds*, its run time is checked.
  A report that "finishes" in one second usually did nothing at all. A
  failure is already a `failed` alert, so it is not checked.
- **`must_start_by`**: either a clock time or a duration.
  - A **duration** (`30m`) counts from the moment the run was due.
  - A **clock time** (`"03:00"`) means that time on the day the run
    **fired**, in the job's time zone, *not* on its run date. The job
    above fires at 02:00 on Monday for Friday's run date, so it must start
    by 03:00 on Monday.

The deadline only counts for a run's first life, and only if the run was
made *before* the deadline. So a backfill of last month, a catch-up after
a long nap, or a rerun never sets off a flood of "you are late" alerts. If
the scheduler is down when a deadline passes, the alert comes as soon as
it starts again.

The timers always follow the config **in force when they ring**. If a
reload moves a job's `must_start_by` or `max_runtime`, the timer moves
with it (earlier or later); if the reload removes the setting, or the
whole job, the timer is called off.

Here is the rule for a clock time, worked out for the job above:

```python
from dataclasses import replace
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pyscheduler.alerts.deadlines import start_deadline
from pyscheduler.engine.model import Run, RunState, Trigger

fired = datetime(2026, 10, 19, 2, tzinfo=UTC)  # Monday 02:00
run = Run(
    run_id="nightly-report.2026-10-16.1",  # Friday's run date
    job="nightly-report",
    run_date=date(2026, 10, 16),
    scheduled_at=fired,
    state=RunState.WAITING,
    reason="",
    attempt=0,
    rerun=0,
    trigger=Trigger.SCHEDULE,
    config_version=1,
    created_at=fired,
    updated_at=fired,
)
job = SimpleNamespace(must_start_by=time(3), timezone=ZoneInfo("UTC"))  # just the two settings
print(start_deadline(run, job))  # 2026-10-19 03:00:00+00:00: Monday, the firing day

backfilled = replace(run, created_at=datetime(2026, 11, 20, tzinfo=UTC))
print(start_deadline(backfilled, job))  # None: made after its deadline, so never late
```

## One Alert, Once

Every alert has an id made of the run, the kind and the attempt:

```text
load.2026-10-16.1:failed:2
```

The monitor remembers the ids it has raised and skips a second alert with
the same id. When it starts, it reads back the ids of the alerts raised
since its memory window began, and every alert of each run still open
(however old), so a restart never repeats an alert for a run it can still
see. Once an hour it forgets the ids of runs that have left memory. A rerun that
fails again gets a new attempt number, so it is a new alert.

Raising an alert always goes in the same order: check the job wants this
kind, check the id is new, **write the alert and its deliveries to the
store**, and only then hand it to the dispatcher. The engine never waits
for an alert: a slow Slack never delays the next job. If the store
cannot write the alert, the engine stops (as for any lost write); if
anything else goes wrong inside the monitor, it is logged and the jobs
carry on.

## A Job's Own `alerts:`

Each job may switch kinds on or off and add its own notifiers:

```yaml
jobs:
  load:
    command: ["python", "load.py"]
    schedule: "0 6 * * *"
    alerts:
      on: [retrying]        # tell me about every retry too
      off: [too_short]      # this one is often quick
      notify: [team-log]    # as well as whatever the routes pick
```

Every kind is on unless turned off, except `retrying`, which is off unless
turned on. If a kind is in both lists, `on` wins.

```python
from pyscheduler.alerts.model import alert_id_for
from pyscheduler.config.model import AlertKind, JobAlerts

alerts = JobAlerts(on=frozenset({AlertKind.RETRYING}), off=frozenset({AlertKind.TOO_SHORT}))
print(alerts.enabled(AlertKind.FAILED))  # True: on by default
print(alerts.enabled(AlertKind.RETRYING))  # True: turned on
print(alerts.enabled(AlertKind.TOO_SHORT))  # False: turned off
print(JobAlerts().enabled(AlertKind.RETRYING))  # False: off by default
print(alert_id_for("load.2026-10-16.1", AlertKind.FAILED, 2))  # load.2026-10-16.1:failed:2
```
