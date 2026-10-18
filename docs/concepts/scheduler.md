# The Scheduler Engine (The School Timetable)

## A Day at School

A school day runs on a timetable. The bell rings at nine, and maths
starts. The science lab can only begin once the teacher has set out
the kit. Only two classes fit in the gym at once. If a lesson is
cancelled, the register says so, and everyone who needed it knows.

The **engine** is PyScheduler's timetable keeper. It rings the bell
when a job is due, checks that everything a job needs has finished,
finds it a free slot, starts it, and writes down what happened.

## One Waiter, Many Tables

Picture a restaurant with one waiter and many tables. The waiter never
stands beside one table waiting for the diners to finish eating. They
take an order, move on, and come back when a table waves. One waiter can
look after a whole room that way.

The engine has one waiter too: PyNet's **event loop**. The "tables" are
small tasks that each wait for something:

```
                 +-------------------+
                 |  the event loop   |   (one waiter)
                 +---------+---------+
        +------------+-----+------+--------------+
        |            |            |              |
  firing task   attempt task  attempt task   store writer
  "sleep until  "wait for     "wait for      "commit the
   the next      job A to      job B to       tray in one
   bell"         end"          end"           trip"
```

- The **firing task** keeps every job's next firing in a heap (the
  earliest on top), sleeps until the top one is due, creates its runs,
  and goes back to sleep.
- One **attempt task** per running attempt waits for its process to
  end. Thousands of them cost almost nothing while they wait.
- The **store writer** carries changes to disk in groups (see
  [Group Commit](group-commit.md)).

## Write It Down, Then Act

A good teacher marks the register *before* sending a pupil to the
office, so nobody wonders later where the pupil went. The engine has
the same rule, called **write-ahead**:

1. change the run in memory (so the rest of the engine sees it at once
   and never starts the same run twice);
2. wait for the store to say the change is safe on disk;
3. only then show it to the API, tell subscribers, and act (for
   example, start the process).

If the computer crashes in the middle, the store never shows a run as
"not started" when it really was started.

## The Life of a Run

1. **Created.** The bell rings (or an upstream run is created, for a
   dependency-only job). The new run starts `scheduled`.
2. **Checked.** Its dependencies are looked up in memory: it is
   `waiting`, `blocked`, `skipped`, or ready (see
   [Dependencies](dependencies.md)).
3. **Overlap.** If the job is already busy, `overlap: queue` makes the
   run wait its turn (keeping only the newest waiting run of each run
   date), `overlap: skip` cancels it, and `overlap: allow` lets it go.
4. **Queued.** It waits for a free slot (see
   [Pools and Limits](pools-and-limits.md)).
5. **Running.** An attempt starts. If it fails and tries remain, the run
   waits and tries again (see [Retries and Backoff](retries-and-backoff.md)).
6. **Final.** It ends `succeeded` or `failed`, and the engine looks
   again at the runs that depend on this job, and only those.

## Try It

The tests drive the engine with a fake clock and a fake launcher, so a
whole day passes in a blink:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile

import pynet.loop

from pyscheduler.config import load_config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher

home = Path(tempfile.mkdtemp())
(home / "pysched.yaml").write_text(f"""
scheduler:
  state_dir: {home / "state"}
  log_dir: {home / "logs"}
jobs:
  bake:
    command: ./bake.sh
    schedule: "0 9 * * *"
  ice:
    command: ./ice.sh
    depends_on: [bake]
""")

config = load_config(home / "pysched.yaml")
clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
engine = Engine(
    config, Store.open(home / "state", sync=False), clock=clock, launcher=FakeLauncher(clock)
)


async def day() -> None:
    await engine.start()
    for _ in range(9 * 60 + 3):  # tick through the morning
        await clock.advance(timedelta(minutes=1))
        await engine.settle()
    ice = engine.book.latest_for("ice", clock.now().date())
    print(ice.run_id, ice.state.value)  # ice.2026-10-19.1 succeeded
    await engine.stop()


pynet.loop.run(day())
```
