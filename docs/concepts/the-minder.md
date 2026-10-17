# The Minder (A Babysitter Who Writes a Note When the Job's Done)

## The Babysitter

When parents go out for the evening, a babysitter stays with the child.
The babysitter does not do the child's homework; they just keep an eye on
things. If the parents phone and say "bedtime!", the babysitter passes the
message on and waits for the child to settle. And before going home, the
babysitter leaves a **note on the kitchen table**: "Went to bed at 8:15,
ate all the peas."

The note is the clever bit. Even if the parents get home very late, long
after the babysitter has left, they can still read exactly how the evening
went.

The **minder** is PyScheduler's babysitter. Every job attempt runs as:

```text
python -m pyscheduler.minder --status run.json --stdout run.out [--stderr run.err] -- ARGV...
```

It:

1. opens the log files, creating their folders and **appending** (old lines are kept);
2. writes `run.json.pid` with its own process id, so the scheduler can check it is still there;
3. runs `ARGV` as its child, with nothing on stdin;
4. passes SIGTERM or SIGINT on to the child, then keeps waiting;
5. writes the note, `run.json`, when the child ends;
6. exits with the child's code, `128 + n` if signal `n` killed the child, or `127` if the program could not start.

Without `--stderr`, the child's stderr goes into the stdout file too.

## The Note

```json
{"exit_code": 3, "signal": null, "started_at": "2026-10-18T09:00:00.000000Z",
 "ended_at": "2026-10-18T09:00:04.250000Z", "start_error": null}
```

- A child killed by a signal has `"exit_code": null` and, say, `"signal": 9`.
- A program that is not there has a `start_error` such as `No such file or directory`.

## Never Half a Note

If the power cut out while the babysitter was halfway through writing, the
parents might find "Went to b". So the minder writes the note on a scrap of
paper first (a temp file in the same folder), makes sure it is really on
the disk (`fsync`), and only then swaps it into place with a rename. A
rename is all-or-nothing: a reader sees the old note or the whole new one.

```python
import json
import subprocess
import sys
import tempfile
from pathlib import Path

folder = Path(tempfile.mkdtemp())
status = folder / "run.json"
minder = [sys.executable, "-m", "pyscheduler.minder", "--status", str(status)]
minder += ["--stdout", str(folder / "run.out")]
job = ["sh", "-c", "echo hi; exit 3"]
code = subprocess.run([*minder, "--", *job], check=False).returncode
print(code)  # 3
print(json.loads(status.read_text())["exit_code"])  # 3
print((folder / "run.out").read_text())  # hi
```

## Why Bother?

The scheduler could run jobs itself, but then if the scheduler crashed, it
would come back with no idea how the jobs it was watching had ended. With
a minder per attempt, the note is waiting on disk when the scheduler
restarts. It costs one small extra Python process per running job.
