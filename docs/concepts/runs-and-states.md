# Runs and States (The Traffic Lights of a Run)

## One Lesson, Many Tries

A job is like "maths" on the timetable. A **run** is one maths lesson on
one day: Monday's maths. If the projector breaks and the lesson starts
again, that is a second **attempt** at the same lesson. And the teacher's
logbook, "9:02 Alice paused the lesson", is a list of **events**.

Every run has an id made of three parts joined by dots:

```
extract-trades.2026-10-17.1
└── job ─────┘ └ run date ┘ └ number
```

The number counts runs of that job on that run date, starting at 1. Job
names can contain `-` and `_` but never `.`, so the id always splits back
cleanly.

## The Traffic Lights

A run moves through **states**, a bit like traffic lights telling it when
it may go:

```
scheduled ──> waiting ──> queued ──> running ──> succeeded
                │  ▲         ▲          │   └──> retry_wait ──> queued
                │  │         │          └──────> failed
                ▼  │         │
              blocked       held  (from waiting / queued / retry_wait)
                         skipped, cancelled  (operator, or on: failure rules)
```

- **scheduled**: the run exists; its time has come.
- **waiting**: red light. It is waiting for the jobs it depends on, or for
  its own previous run to finish.
- **blocked**: a job it needs ended badly, so it cannot go.
- **queued**: amber. Ready to go, waiting for a free slot.
- **running**: green. An attempt is running right now.
- **retry_wait**: the attempt failed, but it may try again after a pause.
- **held**: an operator pressed pause. `held_from` remembers where it was,
  so "release" puts it back.
- **succeeded**, **failed**, **skipped**, **cancelled**: the journey is
  over. These are the **final** states (`FINAL_STATES`).

Each run also remembers its `trigger` (what made it: `schedule`,
`dependency`, `manual`, `backfill` or `catch_up`), and each attempt its
`outcome` (`succeeded`, `failed`, `timeout`, `killed`, `lost` or
`start_error`).

## Frozen Records

Runs, attempts and events are **frozen**: nobody changes one in place.
To move a run on, the engine makes a changed copy with
`dataclasses.replace` and saves that. A copy someone already holds never
changes under their feet.

```python
import dataclasses
from datetime import UTC, date, datetime

from pyscheduler.engine.model import Run, RunState, Trigger, make_run_id, parse_run_id

now = datetime(2026, 10, 17, 9, tzinfo=UTC)
run = Run(
    run_id=make_run_id("extract-trades", date(2026, 10, 17), 1),
    job="extract-trades",
    run_date=date(2026, 10, 17),
    scheduled_at=now,
    state=RunState.SCHEDULED,
    reason="",
    attempt=0,
    rerun=0,
    trigger=Trigger.SCHEDULE,
    config_version=1,
    created_at=now,
    updated_at=now,
)
done = dataclasses.replace(run, state=RunState.SUCCEEDED)
print(run.run_id, done.is_final)  # extract-trades.2026-10-17.1 True
print(parse_run_id(run.run_id))  # ('extract-trades', datetime.date(2026, 10, 17), 1)
```
