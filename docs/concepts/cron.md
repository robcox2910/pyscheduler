# Cron Expressions (The Alarm Clock With Many Dials)

## The Alarm Clock

Imagine an alarm clock with five dials: minute, hour, day of the month,
month, and day of the week. Each dial says which values may ring the bell.
A star `*` means "any". Set the dials once and the clock rings at every
moment that fits.

```text
 minute  hour  day-of-month  month  day-of-week
   */15   9-17       *          *     mon-fri
```

That reads: every 15 minutes, from 9:00 to 17:45, on weekdays.

## Reading the Dials

| Writing | Meaning |
| --- | --- |
| `5` | just 5 |
| `1,15` | 1 and 15 |
| `9-17` | 9 up to 17 |
| `*/15` | every 15th value: 0, 15, 30, 45 |
| `1-30/10` | 1, 11, 21 |
| `jan`, `mon` | names work for months and weekdays (any case) |

Sunday can be written `0`, `7` or `sun`. Macros save typing: `@hourly`,
`@daily`, `@weekly`, `@monthly` and `@yearly`.

## Special Days

- `L` in day-of-month is the last day of the month (`1,15,L` works too).
- `mon#2` is the second Monday of the month.
- `friL` (or `5L`) is the last Friday of the month.

## The OR Rule

If you restrict both day-of-month and day-of-week, a day matches if
**either** one fits. `0 0 13 * fri` means "the 13th, **or** any Friday",
not "Friday the 13th". If only one of the two is restricted, only that one
counts. The month dial always has to fit. A field is "restricted" unless it
is exactly `*`, so `*/1` counts as restricted.

## Try It

```python
from datetime import date

from pyscheduler.schedule.cron import CronExpr

cron = CronExpr.parse("0 0 13 * fri")
print(cron.matches_day(date(2026, 10, 16)))  # a Friday: True
print(cron.matches_day(date(2026, 11, 13)))  # the 13th, a Friday too: True
print(cron.matches_day(date(2026, 10, 15)))  # neither: False
print(CronExpr.parse("*/30 9-10 * * *").times())
```

Bad dials raise a `ValueError` that names the field, for example
`minute: 60 is out of range 0-59`.
