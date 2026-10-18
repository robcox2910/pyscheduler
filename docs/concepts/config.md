# Config Files (The Recipe Book)

## Scribbles and the Clean Copy

Imagine a recipe book that lots of cooks share. People scribble recipes on
cards: some in a hurry, some with spelling mistakes. Before a card goes in
the book, a careful clerk reads it, checks every line and copies it out
neatly. If something is wrong, the clerk writes it on a list and **keeps
going**, so you get the whole list in one go instead of one complaint at a
time.

PyScheduler's config loader is that clerk. Your YAML file is the scribbled
card. The loader turns it into a `Config`: a clean, read-only copy that the
rest of the scheduler cooks from.

```python
import tempfile
from pathlib import Path

from pyscheduler.config import build_config

text = """
defaults:
  retries: {count: 2}
jobs:
  say-hello:
    command: echo hello ${WHO}
    schedule: {at: "09:00"}
  oops:
    comand: echo typo
    enabled: no
"""
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "scheduler.yaml"
    path.write_text(text)
    config, problems = build_config(path, env={"WHO": "world"})

print(config.jobs["say-hello"].command)  # echo hello world
print(config.jobs["say-hello"].retries.count)  # 2, from defaults
for problem in problems:
    print(problem.message)
# unknown key 'comand' in job oops; did you mean 'command'?
# enabled must be true or false, got 'no'
```

## What the Clerk Checks

- **Unknown keys**, with a "did you mean" hint for near misses.
- **Types**: `enabled` must be exactly `true` or `false` (so `no` is never
  quietly read as false), numbers must be numbers, durations look like
  `90s`, `15m` or `1h30m`, and times look like `HH:MM`.
- **`${NAME}` variables** are filled from the environment; a missing one is
  a problem at its line.
- **Names** of jobs, calendars and pools use only letters, digits, `_` and
  `-`.

## Defaults and Includes

`defaults:` is laid under every job like a background sheet: wherever a job
writes a key, the job's value shows; elsewhere the default shows through.
Nested mappings merge key by key too: `retries`, `env` and `pools` one
level deep, and `alerts` at every level. So defaults of `env: {A: "1"}`
and a job's `env: {B: "2"}` give the job both `A` and `B`. Lists such as
`tags` are replaced whole, not joined.

`include: ["jobs/*.yaml"]` pulls in more files (relative to the file that
includes them, in sorted order). A job name used twice is a problem that
names both places.

`load_config` does the same work but raises a `ConfigError` listing every
problem if there are any, so a broken config never starts running. It also
runs the whole-config checks described in [Validation](validation.md).
