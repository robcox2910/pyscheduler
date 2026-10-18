# Operator Actions (The Train Controller's Desk)

## The Controller's Buttons

Trains mostly run to the timetable on their own. But a railway also has
a controller at a desk full of buttons, for the days when something
goes wrong. They can add an extra train, send one round the loop again,
hold one at the platform, stop one that is going wrong, wave one through
a red signal, or cross one off the board.

PyScheduler's engine has the same desk. Every button press is written in
the logbook (the `events` table) with **who** pressed it, **when**, and
the run's state **before** and **after**, and the logbook line is safely
on disk *before* anything moves (see
[The Scheduler Engine](scheduler.md)).

## The Buttons

| Action | Allowed when the run is | What happens |
| --- | --- | --- |
| `trigger(job)` | (makes a new run) | A manual run now. The run date is today in the job's time zone unless given. |
| `rerun(run, downstream=)` | final | Back to `scheduled` with a fresh retry budget. With `downstream`, the final runs of the jobs that depend on it (same run date) reopen too, all the way down. |
| `hold(run)` | scheduled, waiting, blocked, queued, retry_wait | Frozen as `held`. It leaves the queue and its retry timer stops. |
| `release(run)` | held | Back where it was, and checked again at once. A run held during its retry wait is queued straight away: the hold counted as the wait. |
| `kill(run)` | running | SIGTERM now, SIGKILL after `kill_grace`. It ends `failed` ("killed by ...") and never retries. |
| `force_start(run)` | scheduled, waiting, blocked, held, retry_wait, queued | Queued now, first in line. Dependencies and `overlap` are ignored, but pools and the global cap are not. |
| `skip(run)` | open and not running | Ends `skipped`: "pretend it worked", so `on: success` downstreams go ahead. |
| `mark_success(run)` / `mark_failed(run)` | not running (final runs too) | Sets the final state by hand, for example to unblock a downstream after fixing things yourself. |
| `backfill(job, start, end)` | (makes new runs) | One run per business day in the range that has no run yet, due at the job's first firing for that day (midnight for jobs with no schedule). At most 366 days. |
| `reload(config)` | (whole scheduler) | Swaps in a new, already validated config. |

A refused press raises `ActionError` with an HTTP-style `status`: 400
for a bad request (like a backfill that ends before it starts), 404 for
an unknown job or run, 409 when the run is in the wrong state, and 503
when the scheduler is stopping or has failed stop.

## One Hand on the Lever at a Time

A train that is pulling into a station is halfway through a change: it
has stopped, but the board does not say so yet. If the controller
pressed "send round again" at that exact moment, two people would be
moving the same train. So each run has its own **lock**. An attempt's
ending holds it while its result is written down, and every button
holds it from the first check until the press has been acted on. A
press that arrives mid-change simply waits its turn, then sees the run
as it really is (a kill that arrives just after the attempt ended is
refused: the run is no longer running).

## Swapping the Timetable (Reload)

A new timetable arrives. Trains already moving finish their trip the
old way. Every other train follows the new timetable at once. Trains of
a line that has closed are cancelled. In PyScheduler terms:

- a run with an attempt **running** keeps the definition it started
  with; if its job was removed or disabled, it does not retry
- every other open run **adopts** the new definition and is checked
  again at once (a queued run goes back to be placed again, unless it
  was force-started)
- open runs of a removed job end `cancelled` ("removed by reload"), and
  of a disabled job `cancelled` ("disabled")
- the new limits and pools apply at once, and the next firings are
  worked out again from now
- the config is stored with the next version number, and one `reload`
  event is written. Loading the very same config again changes nothing.

## Try It

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
    schedule: manual
  ice:
    command: ./ice.sh
    depends_on: [bake]
""")

config = load_config(home / "pysched.yaml")
clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
engine = Engine(
    config, Store.open(home / "state", sync=False), clock=clock, launcher=FakeLauncher(clock)
)


async def shift() -> None:
    await engine.start()
    bake = await engine.trigger("bake", actor="alice")  # an extra train
    ice = engine.book.latest_for("ice", bake.run_date)
    await engine.hold(ice.run_id, actor="alice")  # wait at the platform
    for _ in range(3):
        await engine.settle()
        await clock.advance(timedelta(minutes=1))
    await engine.release(ice.run_id, actor="alice")  # off you go
    for _ in range(3):
        await engine.settle()
        await clock.advance(timedelta(minutes=1))
    print(engine.book.get(ice.run_id).state.value)  # succeeded
    await engine.stop()


pynet.loop.run(shift())
```
