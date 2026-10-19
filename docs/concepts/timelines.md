# Timelines (The Train Timetable With Ghost Trains)

## The Departures Board

At a railway station there is a big timetable of the day. Trains that
have already gone show when they *really* left and arrived, and a late
one is longer than planned. Trains still to come are drawn as pale
**ghost trains** at the time the timetable promises. A thin line marks
"now".

PyScheduler's `/timeline` is that board. Each job is a lane (a track).
Each attempt is a coloured bar (green succeeded, red failed, blue
running). A run's retries sit side by side. A bar that is still running
stretches to the "now" line. The forecast adds ghost bars (dashed
outline) for the part of the window that is still in the future.

## Zoom

| Zoom | The window |
| --- | --- |
| `hour` | the last hour and the next hour, centred on now |
| `day` | midnight to midnight, local time, on the date asked for (the default is today) |
| `week` | seven local days from the date asked for |

A week is the longest window. Lanes only exist for jobs that have an
attempt or a forecast entry in the window, sorted by name; the axis has
ticks every 10 minutes, every hour or every day. Previous and next
links step by a day (or a week); they are left out at the ends of the
calendar the pages can draw (1900-01-01 to 2999-12-31).

## The Day the Clocks Change

A "day" is midnight to midnight on the *wall clock*, so on the day
clocks go back the window is 25 hours long, and on the day they go
forward it is 23. Positions are worked out from the real elapsed time,
so bars do not drift. Because a run for a date can start the evening
before, the page also reads the runs of the day before the window.

## Try It

```python
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from pyscheduler.ui.timeline_window import Zoom, window_for

london = ZoneInfo("Europe/London")
now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # the day the clocks go back

day = window_for(Zoom.DAY, date(2026, 10, 25), now, london)
print(day.span)  # 1 day, 1:00:00  (25 hours)

hour = window_for(Zoom.HOUR, date(2026, 10, 25), now, london)
print(hour.span, hour.fraction(now))  # 2:00:00 0.5

week = window_for(Zoom.WEEK, date(2026, 10, 25), now, london)
print(week.span)  # 7 days, 1:00:00
```
