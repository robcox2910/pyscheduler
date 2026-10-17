# Time Zones and DST (The Clocks Change, and the Alarm Still Rings Once)

## The Alarm Clock on Clock-Change Night

Twice a year the clocks change. In spring they jump forward an hour, so
in London the time goes 00:59, then **02:00**: half past one never
happens. In autumn they go back an hour, so 01:00 to 01:59 happens
**twice**.

Imagine your alarm is set for 01:30. What should it do?

- **Spring (a gap):** 01:30 is skipped. The alarm rings at the first real
  moment after the gap: the instant the clocks jumped (02:00 local).
- **Autumn (a fold):** 01:30 happens twice. The alarm rings the **first**
  time only.

Either way, the alarm rings exactly once. PyScheduler reads each
schedule's times in the job's own time zone, then turns them into UTC
instants straight away, so nothing else in the scheduler has to worry
about clocks changing.

## Four Kinds of Schedule

| Schedule | Fires |
| --- | --- |
| `CronSchedule` | at a cron expression's times on the days it matches |
| `AtSchedule` | at fixed local times every day |
| `EverySchedule` | every step of *real* time between a start and an end (end included) |
| `BusinessDaySchedule` | at fixed times on the n-th business day of the month (`-1` is the last) |

Each one can also be limited by `days` (weekday numbers, 0 is Monday) and
`months` (1 to 12).

## Every 15 Minutes Means Every 15 Real Minutes

An `every` schedule counts real time, like a stopwatch, not wall-clock
slots. On the night the clocks go back, "every 15 minutes between 00:00
and 03:00" in London fires **17** times: the 13 wall-clock slots plus 4
more for the hour that happens twice.

A window may cross midnight, like 22:00 to 02:00. All its firings belong
to the day the window **opened**, and the next day's window opens at
22:00 the next evening.

## Firings and Run Dates

A **firing** is one ring of the alarm: a UTC instant and the **run date**
it is for. The job's business calendar decides which days fire at all.
With `run_date: previous_business_day`, Monday's run is about Friday's
work, just like homework handed in on Monday is the work set on Friday.

If two schedules land on the same instant, the job fires only once.

## Try It

```python
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from pyscheduler.calendars import ALL_DAYS
from pyscheduler.schedule.cron import CronExpr
from pyscheduler.schedule.firings import RunDateRule, firings_on, next_firing, resolve_local
from pyscheduler.schedule.specs import CronSchedule, Schedule

london = ZoneInfo("Europe/London")
print(resolve_local(date(2027, 3, 28), time(1, 30), london))  # gap: 01:00 UTC
print(resolve_local(date(2026, 10, 25), time(1, 30), london))  # fold: 00:30 UTC


@dataclass(frozen=True)
class Job:
    schedules: tuple[Schedule, ...]
    timezone: ZoneInfo
    run_date_rule: RunDateRule = RunDateRule.SAME_DAY


job = Job((CronSchedule(CronExpr.parse("30 1 * * *")),), london)
print(firings_on(job, date(2026, 10, 25), ALL_DAYS))  # one firing, not two
print(next_firing(job, ALL_DAYS, datetime(2026, 10, 25, 1, tzinfo=UTC)))
```

`firings_between(job, calendar, start, end)` lists every firing after
`start` and up to `end`, one at a time. `next_firing` gives up after
four years (1464 days) and returns `None`, which is how PyScheduler knows
a schedule like `0 0 30 2 *` (30 February) never fires.
