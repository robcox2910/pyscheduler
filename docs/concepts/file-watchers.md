# File Watchers (Waiting at the Letterbox Until the Parcel Stops Rustling)

## Waiting for a Parcel

Imagine you are expecting a parcel. You do not stand with your nose on the
letterbox all day: every so often you go and **look**. And when something
is there, you do not grab it at once if the postman is still pushing it
through, rustling. You wait until it has **stopped moving** for a moment.
Only then do you shout "it's here!", and the people waiting for the parcel
can open it.

A **file watcher** job is that friend at the letterbox. It runs no
command. It looks for a file every `poll`, and it only trusts a file once
its size, modified time and inode (which file it really is) have stayed
the same for `min_age`: a file still being copied keeps changing, so it
is not ready yet, and a file swapped for another starts its wait again.
`min_age` may not be longer than `give_up_after` (the validator says so),
or the watcher could never succeed.

```yaml
jobs:
  trades-in:
    type: file_watcher
    schedule: "0 6 * * 1-5"
    watch: "/data/in/trades_{run_date:%Y%m%d}.csv"
    min_age: 30s        # still for this long counts as "arrived" (default 30s)
    poll: 10s           # how often to look (default 10s)
    give_up_after: 4h   # stop waiting after this (default 1d)
  load-trades:
    command: "python load.py {files}"
    depends_on: [trades-in]
```

## Patterns: `watch` Is a Glob

`watch` is a **glob**: `*` matches any run of characters and `?` matches
one, so `/data/in/trades_*.csv` matches every trades file. It is a template
too, so `{run_date:%Y%m%d}`, `{job}`, `{run_id}` and the other blanks work
(not `{attempt}` or `{files}`: there is no attempt number or file yet when
the pattern is filled in). Folders are never matched, only files. A
relative pattern (`inbox/*.csv`) is looked up from the folder the
scheduler was started in, unlike `state_dir` and `log_dir`, which are
next to the config file: write an absolute path to be sure.

When a look finds at least one match that has been still for `min_age`,
the watcher succeeds. Its **files** are every match that was still at that
look, sorted. A `min_age` of `0s` means "still on first sight".

## Handing the Files On: `{files}` and `PYSCHED_FILES`

Every job that depends **directly** on a watcher is told what it found:

- the `{files}` blank holds the paths quoted for the shell
  (`shlex.join`), ready for a command line;
- the `PYSCHED_FILES` environment variable holds them one per line.

`{files}` is only allowed in jobs that depend directly on a watcher (the
validator says so otherwise). The files are written to the store with the
run, so a rerun of `load-trades` next week, or after a restart, still gets
them.

## Giving Up

If nothing has arrived and stayed still by `give_up_after` (counted from
the attempt's start), the watcher gives up. The attempt ends with outcome
`timeout` and the reason `gave up: no stable file after 4h`, and a
`watcher_timeout` alert is raised. The job's normal `retries:` apply, so
a retry waits again from scratch. If the run then ends failed, the usual
`failed` alert comes too (routes can pick which ones they want), and the
jobs that depend on it are blocked.

An operator `kill` stops a watcher at once: the run ends failed, `killed by
<name>`.

## Why a Watcher Takes No Slot

A watcher spends nearly all its time doing nothing but waiting. If it held
one of the `max_running` places, three watchers waiting all night could
stop every real job from running. So a watcher takes **no global slot** and
cannot use pools. Instead, the looks at the disk share a small limit of
their own, `scheduler.watch_limit` (default 8): at most that many watchers
are checking files at the same moment, and the rest wait their turn for a
few milliseconds. A reload that changes `watch_limit` takes effect as
the watchers already waiting finish: they keep sharing the old limit,
and every watcher that starts after the reload shares the new one.

A reload that removes a watcher's job, disables it, or turns it into a
command job stops a waiting watcher at once (it has no work to lose):
its run ends `cancelled`, with the reason `removed by reload` or
`disabled`.

## Restarts

A watcher has no process, so there is nothing to lose when the scheduler
stops. When it starts again, a watcher that was waiting simply **resumes**
looking, and its give-up deadline still counts from when the attempt first
started: a 1-minute watcher stopped after 40 seconds gives up 20 seconds
after the restart, not a minute later.

Nobody was watching while the scheduler was down, so the first look after
a restart trusts each file's modified time: a file that has not changed
since well before now counts as having been still all that time. Even
when the deadline passed during the downtime, a file that had been still
for `min_age` *by the deadline* makes the watch succeed; the watcher only
gives up if no file was ready in time.

The modified time is the file's own claim, so a copy that keeps the
original's time (`cp -p`, `rsync -t`) and is still being written when the
scheduler comes back can look finished at that first look. If files reach
you that way, write them under a temporary name and rename them into
place when they are complete, so the watch pattern only ever sees whole
files.

## What a Watcher Does Not Take

A watcher job may not have `command`, `cwd`, `env`, `timeout`, `pools`,
`stdout` or `stderr`. If those keys come from the `defaults:` section,
they are simply ignored for watchers (they are meant for the command
jobs). The other way round, `type` and `watch` cannot be set in
`defaults:` at all: each watcher says so itself. A watcher's progress lines
("1 match(es)", "found ...") go to its log, which `pysched log RUN` shows
like any other.

## Try It

The watcher below looks at a folder with a file already in it. With
`min_age` of zero the file counts at the first look, and the watch ends at
once with the file it found:

```python
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pynet.loop
from pynet.loop import Semaphore

from pyscheduler.config.model import WatchSpec
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.watcher import WatcherHandle

folder = Path(tempfile.mkdtemp())
(folder / "trades_20261019.csv").write_text("id,amount\n1,42\n")
spec = WatchSpec(
    pattern=str(folder / "trades_*.csv"),
    min_age=timedelta(0),
    poll=timedelta(seconds=10),
    give_up_after=timedelta(hours=4),
)


async def main() -> None:
    """Watch once and show what was found."""
    clock = FakeClock(datetime(2026, 10, 19, 6, tzinfo=UTC))
    handle = WatcherHandle(
        pattern=spec.pattern,
        spec=spec,
        started_at=clock.now(),
        clock=clock,
        limit=Semaphore(8),
        log_path=folder / "watch.log",
    )
    result = await handle.wait()
    print(result.succeeded, [Path(f).name for f in handle.files])


pynet.loop.run(main())
# True ['trades_20261019.csv']
```
