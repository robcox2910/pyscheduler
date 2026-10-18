# Dependencies (You Can't Ice the Cake Before It's Baked)

## Bake First, Ice Second

Icing a cake before it has been baked makes a mess. So the icing job
says: "I depend on the baking job." The scheduler keeps the icing run
**waiting** until baking is finished.

In config it looks like this:

```yaml
jobs:
  ice-cake:
    command: ./ice.sh
    depends_on:
      - bake-cake                 # same as: {job: bake-cake, on: success}
      - {job: order-flour, on: done, run_date: -1}
```

Each entry says three things: *which* job, *what result counts* (`on`),
and *which day's run* to look at (`run_date`, in business days back).

## Three Kinds of "On"

| `on`      | Good enough when the upstream...          | Otherwise              |
|-----------|-------------------------------------------|------------------------|
| `success` | succeeded or was skipped                  | failed/cancelled: **blocked** |
| `failure` | failed (a clean-up job for bad days)      | anything else: **skipped**    |
| `done`    | ended in any final state                  | (never otherwise)      |

If the upstream has no run yet, or is still going, the answer is always
**wait**. Nothing is decided until the upstream is finished.

## Blocked vs Skipped

- **Blocked** means "the thing I needed went wrong." The run stops and an
  operator can fix the upstream and rerun it.
- **Skipped** means "the thing I was waiting to clean up after never
  happened." A `on: failure` job has nothing to do on a good day, so it
  quietly skips.

## Which Day Is "Yesterday"?

`run_date: -1` means "one *business* day back", using the job's calendar.
On Monday, that is Friday, not Sunday:

```python
from datetime import date
from pyscheduler.calendars import BusinessCalendar

uk = BusinessCalendar("uk", frozenset(range(5)), frozenset(), frozenset())
print(uk.shift(date(2026, 10, 19), -1))  # 2026-10-16, the Friday
```

## Many Dependencies

When a job has several, each gets its own verdict and they are combined:
**skip** beats **block**, which beats **wait**, which beats **ready**.
The reason shown is from the first dependency (in the order you wrote
them) with the winning verdict.

## The Run Book

The engine keeps every run in a **run book**: a register with tabs, so
"what is the latest run of `bake-cake` for Monday?" is a lookup, not a
search through every run. The book also keeps a second *published* copy
of each run that only changes once the new state is safely saved, and
that is the only copy the API shows.

```python
from pyscheduler.engine.runbook import RunBook

book = RunBook()
print(book.open_runs())  # []
```
