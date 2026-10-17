# Clocks and Launchers (A Practice Clock and a Starting Whistle)

## The Practice Clock

Imagine practising a school day at home. You do not want to wait a real
hour for the bell to ring after maths. So you use a **practice clock**:
a toy clock whose hands only move when you turn the knob. "Turn it to
nine... the bell rings. Turn it to ten... the next bell rings." A whole
day takes a minute.

PyScheduler's engine never looks at the computer's clock directly. It asks
a **`Clock`**, which can do three things:

- `now()` — what time is it? (always an aware UTC `datetime`)
- `call_at(when, callback)` — ring this bell at `when`; it returns
  something with `cancel()` in case you change your mind;
- `await sleep_until(when)` — have a nap until `when`.

There are two clocks:

- **`SystemClock`** is the real wall clock. It sets a loop timer for at
  most `MAX_NAP` (60) seconds, then looks at the wall clock again. Naps
  keep it honest if the computer's clock is changed or a laptop sleeps.
- **`FakeClock`** is the practice clock. Its hands move only when a test
  calls `advance(...)` or `advance_to(...)`. Bells ring one at a time in
  time order (two bells at the same moment ring in the order they were
  set), and the clock shows each bell's own time while it rings. After
  each bell, everyone woken gets a few turns (`SETTLE_TURNS`) to react.

```python
from datetime import UTC, datetime, timedelta

import pynet.loop

from pyscheduler.engine.clock import FakeClock

clock = FakeClock(datetime(2026, 10, 19, 9, tzinfo=UTC))


async def main() -> None:
    clock.call_at(clock.now() + timedelta(hours=2), lambda: print("lunch", clock.now()))
    clock.call_at(clock.now() + timedelta(hours=1), lambda: print("break", clock.now()))
    await clock.advance(timedelta(hours=3))  # a whole morning in a blink


pynet.loop.run(main())
# break 2026-10-19 10:00:00+00:00
# lunch 2026-10-19 11:00:00+00:00
```

## The Starting Whistle

A PE teacher starts a race with a whistle, then watches for each runner to
cross the line. The engine does the same with a **`Launcher`**: it hands
over a `LaunchSpec` (the command, its folder, its environment, where the
logs and the status note go) and gets back an **`AttemptHandle`**. With
the handle it can:

- `await wait()` for an `AttemptResult` (exit code, signal, start and end
  times, and a `start_error` if the job never got going); `succeeded` is
  true only for exit 0 with no signal and no start error;
- `terminate()` (SIGTERM) or `kill()` (SIGKILL) the attempt. Both signal
  the **whole process group**, so a shell's background `sleep &` is
  stopped too, not left behind.

**`MinderLauncher`** is the real launcher. It runs every attempt under
[the minder](the-minder.md) in a new session, after clearing away any old
status note. When the minder ends, it reads the minder's note with
`read_status`. If there is no note (say the minder itself was killed with
SIGKILL), it works the answer out from the minder's own exit status. A
note that is garbled never crashes anything: it comes back as a
`start_error` beginning `corrupt status file:`.

```python
import os
import tempfile
from pathlib import Path

import pynet.loop

from pyscheduler.engine.launcher import LaunchSpec, MinderLauncher

folder = Path(tempfile.mkdtemp())
spec = LaunchSpec(
    run_id="hello.2026-10-19.1",
    attempt=1,
    argv=("sh", "-c", "echo hi; exit 4"),
    cwd=None,
    env=dict(os.environ),
    stdout_path=folder / "hello.out",
    stderr_path=None,
    status_path=folder / "hello.json",
)


async def main() -> None:
    handle = await MinderLauncher().launch(spec)
    result = await handle.wait()
    print(result.exit_code, result.succeeded)  # 4 False


pynet.loop.run(main())
print((folder / "hello.out").read_text())  # hi
```

## The Pretend Race

For tests, `pyscheduler.testing.FakeLauncher` runs pretend races on a
`FakeClock`. You tell it how each job's attempts should go with
`plan(job, FakeResult(...), ...)`; the results are used in order and the
last one repeats. A pretend attempt finishes when the fake clock reaches
its start plus its `duration`. `terminate()` ends it at once with signal
15 (unless it `ignores_term`), and `kill()` always ends it with signal 9.

```python
from datetime import UTC, datetime
from pathlib import Path

import pynet.loop

from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.launcher import LaunchSpec
from pyscheduler.testing import FakeLauncher, FakeResult

clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
launcher = FakeLauncher(clock)
launcher.plan("report", FakeResult(exit_code=1), FakeResult())  # fail, then pass
spec = LaunchSpec(
    "report.2026-10-19.1", 1, ("make-report",), None, {}, Path("r.out"), None, Path("r.json")
)


async def main() -> None:
    first = await launcher.launch(spec)
    await clock.advance_to(datetime(2026, 10, 19, 0, 1, tzinfo=UTC))
    print((await first.wait()).exit_code)  # 1
    second = await launcher.launch(spec)
    await clock.advance_to(datetime(2026, 10, 19, 0, 2, tzinfo=UTC))
    print((await second.wait()).succeeded)  # True


pynet.loop.run(main())
```

## Why Bother?

Because the engine only ever talks to a `Clock` and a `Launcher`, the very
same engine code runs for real (wall clock, real processes) and in tests
(practice clock, pretend races). A test can check a whole day of
schedules, retries and timeouts in milliseconds, and get the same answer
every time.
