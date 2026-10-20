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

## The `agents:` Section

An optional top-level `agents:` section turns on [remote
agents](remote-agents.md): jobs with `agent:` or `agent_tags:` run on
`pysched-agent` workers instead of on the scheduler.

```yaml
agents:
  listen: "127.0.0.1:8471"   # embed a broker here, or...
  # broker: "queue.example:5672"   # ...use one that is already running
  secrets_file: agents.yaml        # required; relative to this file
  agent_grace: 60s                 # silence before an agent is offline (default 60s)
  lost_after: 10m                  # silence before an attempt is lost (default 10m)
```

- Set `listen` **or** `broker`, not both. `secrets_file` is required: a
  list of `{name, secret}` lines, secrets at least 32 characters, file
  readable by its owner only (a warning if others can read it).
- A job's `agent` must name an agent in the secrets file; `agent` and
  `agent_tags` together, or on a file watcher, are problems.
- A reload applies a changed secrets file, but changing `listen`,
  `broker`, `agent_grace` or `lost_after` needs a restart.
- Secrets never appear in the API, the UI or the config history.
- The broker has no authentication or encryption: use loopback, a trusted
  network or a tunnel. See [Signed Messages](signed-messages.md).
