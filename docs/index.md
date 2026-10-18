# What Is a Job Scheduler?

## The School Timetable

A school runs on a timetable. Maths is at nine, lunch is at twelve, and
the science lab can only start once the teacher has set out the kit. The
bell rings on time whether or not anyone is looking at a clock.

A **job scheduler** is a timetable for computer jobs. It knows *what* to
run, *when* to run it, *what must finish first*, and *who to tell* when
something goes wrong.

PyScheduler is a small, from-scratch scheduler in the style of Autosys
and Control-M, built to be read and learned from. It runs on our own
libraries: [pynet](https://github.com/robcox2910/pynet) (event loop and
processes), [pyweb](https://github.com/robcox2910/pyweb) (the HTTP API)
and [pydb](https://github.com/robcox2910/pydb) (the journalled store).

## Try It

The repository has a small nightly ETL chain in `examples/etl.yaml`: a
calendar, a pool, a flaky job that is retried, an on-failure clean-up and
`previous_business_day` run dates. From the repository root:

```text
pysched validate examples/etl.yaml
pysched serve examples/etl.yaml        # leave this running
pysched trigger extract --date 2026-10-16
pysched trigger fetch-rates --date 2026-10-16
pysched status --date 2026-10-16
```

Or, without starting anything, ask what the example would do:

```python
from pathlib import Path

from pyscheduler.config import load_config

config = load_config(Path("examples/etl.yaml"), env={})
for name, job in config.jobs.items():
    print(name, [dep.job for dep in job.depends_on])
# extract []
# fetch-rates []
# transform ['extract']
# load ['transform', 'fetch-rates']
# cleanup ['load']
```

## Learning Path

Read these in order; each page starts with an everyday picture and ends
with something you can run.

1. [The Scheduler Engine (The School Timetable)](concepts/scheduler.md): What the engine does all day: ring the bell, check, start, write down.
2. [YAML (A Recipe Card With Indents)](concepts/yaml.md): How config files are written and read.
3. [Config Files (The Recipe Book)](concepts/config.md): Turning the YAML into a checked, read-only config.
4. [Templates and Values (Form Letters and Labels)](concepts/templates.md): `{run_date}` placeholders and `${SECRET}` variables.
5. [Cron Expressions (The Alarm Clock With Many Dials)](concepts/cron.md): When a job fires.
6. [Calendars and Run Dates (School Days and Homework)](concepts/calendars-and-run-dates.md): Business days, holidays and which day a run is for.
7. [Time Zones and DST (The Clocks Change, and the Alarm Still Rings Once)](concepts/time-zones-and-dst.md): Firing exactly once when the clocks change.
8. [Dependencies (You Can't Ice the Cake Before It's Baked)](concepts/dependencies.md): Waiting for other jobs, and on-failure clean-ups.
9. [Runs and States (The Traffic Lights of a Run)](concepts/runs-and-states.md): Queued, running, succeeded, failed, blocked...
10. [Retries and Backoff (Knock Again, but Wait a Bit Longer Each Time)](concepts/retries-and-backoff.md): Trying again after a failure.
11. [Pools and Limits (Only Two Kids on the Trampoline)](concepts/pools-and-limits.md): How many jobs may run at once.
12. [The Minder (A Babysitter Who Writes a Note When the Job's Done)](concepts/the-minder.md): The small program that runs each attempt.
13. [Clocks and Launchers (A Practice Clock and a Starting Whistle)](concepts/clocks-and-launchers.md): Real and fake clocks; how attempts are started.
14. [Group Commit (One Trip to the Post Box for Many Letters)](concepts/group-commit.md): Saving many changes in one trip to the disk.
15. [Concurrency (One Waiter, Two Hundred Tables)](concepts/concurrency.md): Proving 200 jobs started together finish fast without the loop stalling.
16. [Recovery and Catch-Up (Waking Up After a Nap and Reading the Babysitter's Notes)](concepts/recovery-and-catch-up.md): Restarting after a crash without losing a run.
17. [Validation (The Teacher Who Checks Your Work Before the Test)](concepts/validation.md): Every mistake in a config, found before it runs.
18. [Operator Actions (The Train Controller's Desk)](concepts/operator-actions.md): Hold, kill, rerun, force and friends.
19. [Forecast (The Weather Report for Jobs)](concepts/forecast.md): What will run tomorrow, worked out in advance.
20. [The HTTP API (The Front Desk With Wristbands)](concepts/http-api.md): Talking to the scheduler over HTTP, with tokens.
21. [The Service and the CLI (Opening the Shop and the Remote Control)](concepts/the-service-and-cli.md): `pysched serve` and the `pysched` commands.
22. [Notifiers (The Ways to Reach Someone)](concepts/notifiers.md): Log lines, commands and messages that carry an alert to a person.
23. [Routing Alerts (The Post Office With a Pigeonhole per Notifier)](concepts/routing-alerts.md): Topics, `*` and `#`, retries, dead letters and replay.
24. [Alerts (The Smoke Alarm and the Teacher Who Notices You Never Arrived)](concepts/alerts.md): Every alert kind, `must_start_by`, `max_runtime`, `min_runtime`, dedupe and a job's `alerts:`.
