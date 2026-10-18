# Pools and Limits (Only Two Kids on the Trampoline)

## The Trampoline Rule

A trampoline is fun, but only two kids can bounce at once or somebody
gets hurt. Everyone else waits in line. When one kid climbs off, the
next kid in line gets on.

PyScheduler works the same way. `max_running` is the number of kids the
trampoline holds. Runs that are ready but do not fit stay **queued**.

## Small Trampolines (Pools)

Some things are scarcer than others. Maybe the database can only
handle one heavy job at a time. A **pool** is a small trampoline for
that one thing:

```yaml
max_running: 4
pools:
  db: 1
jobs:
  load-warehouse:
    command: ./load.sh
    pools: {db: 1}
```

A run that needs `db` waits for a free `db` slot. A run that does not
need it is not held up: it can start as soon as the big trampoline has
room.

## Who Gets On First?

The line is ordered by **criticality** (most important first), then by
the time the run was due, then by run id so ties are always settled the
same way. If the first in line does not fit, the scheduler skips them
and tries the next, so one busy pool never blocks everyone.

## Try It

```python
from datetime import UTC, date, datetime

from pyscheduler.config.model import Criticality
from pyscheduler.engine.admission import Admission, ReadyQueue
from pyscheduler.engine.model import Run, RunState, Trigger

now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def run(name: str) -> Run:
    return Run(
        name,
        name,
        date(2026, 10, 19),
        now,
        RunState.QUEUED,
        "",
        0,
        0,
        Trigger.SCHEDULE,
        1,
        now,
        now,
    )


admission = Admission(max_running=2, pools={"db": 1})
queue = ReadyQueue()
queue.push(run("a"), Criticality.NORMAL, {"db": 1})
queue.push(run("b"), Criticality.NORMAL, {"db": 1})
queue.push(run("c"), Criticality.NORMAL, {})

print([r.run_id for r in queue.take_startable(admission)])  # ['a', 'c']
admission.release({"db": 1})  # a finished
print([r.run_id for r in queue.take_startable(admission)])  # ['b']
```

`b` had to wait for the `db` slot, but `c` was not held up behind it.

## Changing the Rules Mid-Game

On a config reload the limits can shrink below what is running now. The
runs already bouncing are never thrown off; nobody new starts until
enough kids have climbed off to get under the new limit.

## Force Start

An operator can force-start a run. It jumps to the front of the line,
but it still has to fit: even the boss cannot squeeze a third kid onto
a two-kid trampoline.
