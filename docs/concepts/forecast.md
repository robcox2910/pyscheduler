# Forecast (The Weather Report for Jobs)

## "Probably Starting Around Six"

A weather forecast doesn't know what will happen. It looks at the pattern
and says "rain around three". The **forecast** does the same for jobs: it
reads the schedules, remembers how long each job usually takes, and says
"this job will probably start around then".

- A **scheduled** job is easy: it starts when its alarm rings.
- A **follower** (a job that waits for others) starts when its slowest
  upstream is expected to *finish*: start plus the typical duration.
- The typical duration is the **median** of the last 10 runs: line them up
  shortest to longest and take the middle one, so one freak slow day
  doesn't spoil the forecast. With no history, the forecast uses the
  upstream's start time.

```python
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pyscheduler.config import load_config
from pyscheduler.schedule.forecast import forecast

text = """
jobs:
  extract:
    command: run-extract
    schedule: "30 18 * * *"
  report:
    command: run-report
    depends_on: [extract]
"""
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "scheduler.yaml"
    path.write_text(text)
    config = load_config(path)

minute = timedelta(minutes=1)
start = datetime(2026, 10, 19, tzinfo=UTC)
end = start + timedelta(days=1)
past = {"extract": [10 * minute, 20 * minute, 30 * minute]}  # newest first
for entry in forecast(config, start, end, durations=past):
    print(entry.job, entry.expected_start.time(), entry.via)
# extract 18:30:00 None
# report 18:50:00 extract
```

## The Small Print

- The window is `(start, end]`: `start` is left out, `end` is included.
  Both must carry a time zone, or you get a `ValueError`.
- Disabled jobs and manual-only jobs are left out.
- A follower gets one entry per run date. It appears as soon as *any*
  upstream has an entry for its date, just like the real scheduler creates
  its run when the first upstream run shows up. Its start is decided by the
  upstream expected to finish last (`via` names it).
- A follower whose upstream is inside the window is kept even if its
  expected start falls after `end`.
- The forecast assumes the happy path: every upstream succeeds. The `on:`
  kind of a dependency is ignored, so a job that only runs on failure still
  appears.
- Jobs caught in a dependency cycle are skipped.

The API (`GET /api/forecast`), `pysched forecast` and the
timeline's ghost bars all use this one function.
