# Calendars and Run Dates (School Days and Homework)

## School Days

A school only runs on **school days**. Saturdays and Sundays are off, so are
bank holidays and snow days, and now and then a Saturday catch-up lesson is
added. A **business calendar** is that list: which weekdays work, which
dates are holidays, and which extra dates count anyway.

```python
from datetime import date

from pyscheduler.calendars import BusinessCalendar

uk = BusinessCalendar(
    "uk",
    weekdays=frozenset(range(5)),  # 0 = Monday ... 4 = Friday
    holidays=frozenset({date(2026, 12, 25), date(2026, 12, 28), date(2027, 1, 1)}),
    extra_days=frozenset(),
)

print(uk.is_business_day(date(2026, 12, 25)))  # False: Christmas Day
print(uk.previous(date(2026, 12, 29)))  # 2026-12-24
print(uk.next(date(2026, 12, 24)))  # 2026-12-29
print(uk.shift(date(2026, 10, 19), -1))  # 2026-10-16 (Monday back to Friday)
print(uk.nth_of_month(2026, 10, -1))  # 2026-10-30: last business day of October
```

`previous` and `next` always move at least one day. `shift(d, n)` takes
`n` school-day steps (negative goes back; zero stays put). `nth_of_month`
counts school days from the start (1, 2, ...) or the end (-1, -2, ...) of a
month, and gives `None` if the month is too short. If a calendar has no
working days at all, `previous` and `next` raise `ValueError` instead of
searching forever.

The built-in `ALL_DAYS` calendar (named `none`) counts every day, and
`merge_calendars` joins the holidays of several calendars into one.

## Which Day's Homework Is This?

A job that runs at 02:00 on Tuesday is usually processing *yesterday's*
work. Like a teacher marking Monday's homework on Tuesday morning, the job
needs to know which day it is really for. That day is the **run date**.

- `same_day`: the run date is the day the job fires. Tuesday's run is for Tuesday.
- `previous_business_day`: the run date is the school day before the firing day.

On a Monday-to-Friday calendar, a 02:00 job with `previous_business_day`:

| Fires on | Run date |
|----------|----------|
| Tuesday | Monday |
| Friday | Thursday |
| Monday | Friday |

The Monday run processes **Friday**, because the weekend has no homework.
The calendar also filters firings: if the firing day itself is not a
business day, the job simply does not fire, so Saturday and Sunday never
produce a second Friday run.

```python
from datetime import date

from pyscheduler.calendars import ALL_DAYS, BusinessCalendar

weekdays = BusinessCalendar("weekdays", frozenset(range(5)), frozenset(), frozenset())
monday = date(2026, 10, 19)
print(monday)  # same_day -> 2026-10-19
print(weekdays.previous(monday))  # previous_business_day -> 2026-10-16
print(ALL_DAYS.previous(monday))  # no calendar -> 2026-10-18
```
