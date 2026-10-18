# Recovery and Catch-Up (Waking Up After a Nap and Reading the Babysitter's Notes)

## Coming Home

A parent pops out for the afternoon and leaves a babysitter in charge.
When they come home, they do not assume nothing happened. They:

1. read the notes the babysitter left ("bath done at 6, all fine");
2. peek into the rooms where someone may still be busy;
3. check the fridge chart for the chores that were missed while they
   were out, and decide which ones are still worth doing;
4. throw away notes so old nobody needs them any more.

The scheduler does exactly this every time it starts. It may have been
stopped politely, or killed in the middle of the night, or the machine
may have lost power. Either way, when it comes back it works out the
true state of every run, starts nothing twice, and carries on.

## Step 1: Load the Register

Everything the scheduler knows is written down in its store before it
acts (the write-ahead rule). On start it reads back:

- every **open** run (not yet succeeded, failed, skipped or cancelled);
- the **final** runs of the last `memory_days` run dates (default 7),
  so dependencies on recent runs can be checked.

## Step 2: Read the Babysitter's Notes

Each attempt runs under a [minder](the-minder.md), a small babysitter
process that lives in its own session. Killing the scheduler does not
kill the minders: they keep looking after their jobs and, when a job
ends, write a **status note** saying how.

For every run that was `running`, the scheduler looks at its latest
attempt:

| What it finds                                   | What it does                           |
|-------------------------------------------------|----------------------------------------|
| a status note                                   | finishes the attempt from the note     |
| no note, but the minder's pid is still alive    | **adopts** it and checks every 2 s     |
| no note and no live minder                      | marks the attempt `lost`               |
| no attempt row at all (crashed mid-commit)      | marks it `lost` (nothing was launched) |

An adopted attempt is treated like any other: it keeps its slot (even
if the limits are lower now), its `timeout` still counts from when it
really started, and an operator `kill` signals its whole process group.
A `lost` attempt follows the normal [retry rules](retries-and-backoff.md),
so a job with retries is simply tried again.

The other open runs are picked up too:

- `queued` runs go back in the queue;
- `retry_wait` runs get their timer back, counted from when their last
  attempt ended (an overdue retry starts at once);
- `held` runs stay held;
- `scheduled`, `waiting` and `blocked` runs are checked again;
- open runs of a job that is no longer in the config are cancelled.

!!! note "A small pid risk"
    "Is the minder alive?" is a plain pid check (`os.kill(pid, 0)`). If
    the minder died and the computer handed its pid to a brand-new
    process, the scheduler would think the minder was still there until
    that process ends. This is rare, and it never starts a job twice.

## Step 3: The Missed Chores

The store remembers `last_tick`, the instant of the latest firing the
scheduler handled. Firings between `last_tick` and now were missed.
Each job's `catch_up` setting decides what happens to them:

| `catch_up`          | Missed firings that get a run         |
|---------------------|---------------------------------------|
| `latest` (default)  | only the most recent one              |
| `all`               | every one (at most the latest 1000)   |
| `none`              | none                                  |

Caught-up runs have `trigger: catch_up`. A firing whose run already
exists is never created again: runs are unique by job and due time.
On the very first start there is no `last_tick`, so nothing is caught
up. The same rule is used if the computer's clock jumps forward while
the scheduler runs (say, a laptop waking from sleep).

```yaml
jobs:
  hourly-prices:
    command: ./prices.sh
    schedule: "0 * * * *"
    catch_up: latest   # down from 01:10 to 04:30? one run, for 04:00
```

## Step 4: Throw Away Old Notes

Once an hour the scheduler **prunes**: final runs whose run date is
older than `retention` (default `90d`) are deleted from the store,
with their attempts and logbook lines, 500 at a time. Open runs are
never pruned, however old. Final runs older than `memory_days` are
also forgotten in memory (they can still be read back from the store).
Log files are left alone: cleaning those is the operator's job.

## Try It

This snippet stops a scheduler while a job is running, starts a new one
on the same state folder, and watches it adopt the minder:

```python
from pathlib import Path
from tempfile import mkdtemp

import pynet.loop

from pyscheduler.config import load_config
from pyscheduler.engine.clock import SystemClock
from pyscheduler.engine.core import Engine
from pyscheduler.engine.launcher import MinderLauncher
from pyscheduler.store import Store

folder = Path(mkdtemp())
config_file = folder / "pysched.yaml"
config_file.write_text(f"""
scheduler:
  state_dir: {folder / "state"}
  log_dir: {folder / "logs"}
jobs:
  nap:
    command: "sleep 1"
    schedule: manual
""")


def engine() -> Engine:
    config = load_config(config_file, env={})
    store = Store.open(config.settings.state_dir)
    return Engine(config, store, clock=SystemClock(), launcher=MinderLauncher())


async def first() -> str:
    e = engine()
    await e.start()
    run = await e.trigger("nap", actor="me")
    await pynet.loop.sleep(0.3)  # let it start
    await e.stop()  # the minder keeps going
    return run.run_id


async def second(run_id: str) -> None:
    e = engine()
    await e.start()  # recovery adopts the minder
    while not e.book.published(run_id).is_final:
        await pynet.loop.sleep(0.1)
    print(run_id, e.book.published(run_id).state)  # succeeded
    await e.stop()


run_id = pynet.loop.run(first())
pynet.loop.run(second(run_id))
```
