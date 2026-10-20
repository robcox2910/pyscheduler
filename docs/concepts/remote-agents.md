# Remote Agents (The Delivery Drivers)

## A Shop With Drivers

Picture a shop that takes orders at the counter. For a small order the
shopkeeper walks round the corner herself. But some parcels have to go to
a warehouse across town, where only one driver has the right van. So the
shopkeeper writes an order slip, seals it in an envelope and posts it in
that driver's pigeonhole. The driver reads the slip, does the delivery and
posts a sealed note back: "started", "still going", "done".

PyScheduler's **remote agents** are those drivers. The scheduler is the
shopkeeper. A job with an `agent:` or `agent_tags:` line is not run on the
scheduler's own machine; it is posted to a worker program, `pysched-agent`,
which runs it on *its* machine and reports back. The pigeonholes are
queues on a [pymq](https://github.com/robcox2910/pymq) broker. Two pages go
deeper: [Heartbeats](heartbeats.md) (how the scheduler knows a driver is
still out there) and [Signed Messages](signed-messages.md) (how each
envelope is sealed).

!!! warning "The broker has no door guard"
    The broker has **no authentication and no encryption**. Anyone who can
    reach its port can read the queues and post a letter. The wax seal on
    every message (see [Signed Messages](signed-messages.md)) stops a
    stranger from *forging* an order or a report, but it does not hide
    anything and it does not stop a stranger reading or flooding the
    queues. So keep the broker on loopback (`127.0.0.1`), on a network you
    trust, or reach it through a tunnel (an SSH or VPN tunnel).

## Placing a Job

Two job keys choose where a job runs:

```yaml
jobs:
  build-app:
    command: ./build.sh
    schedule: manual
    agent: build-01          # this agent, by name
  train-model:
    command: ./train.sh
    schedule: manual
    agent_tags: [gpu]        # any online agent that has ALL these tags
```

Use one or the other, never both. A job with neither runs on the
scheduler, as before. If no agent fits (none online, or all full) the run
waits, queued, until one does; it never silently runs somewhere else. A
**file watcher job cannot set `agent` or `agent_tags`**: it only waits for
files to land and would hold an agent's slot for nothing, so validation
refuses it.

## The `agents:` Section

```yaml
agents:
  listen: "127.0.0.1:8471"     # embed a broker here...
  # broker: "queue.example:5672"  # ...or use one that already runs (not both)
  secrets_file: agents.yaml    # who may be an agent, and their secrets
  agent_grace: 60s             # silence allowed before an agent counts as offline
  lost_after: 10m              # how long a job may go unheard before it is lost
```

The secrets file is a list, one line per agent, each secret at least 32
characters; keep it readable by its owner only (`chmod 600`):

```yaml
- {name: build-01, secret: 0123456789abcdef0123456789abcdef0123}
- {name: gpu-01, secret: fedcba9876543210fedcba9876543210fedc}
```

Start an agent on each machine with the same name and secret:

```text
pysched-agent --broker sched.example:8471 --name gpu-01 --tags linux,gpu --secret-file s.txt
```

## What Happens When Things Go Wrong

- **Lost attempts.** If the scheduler hears nothing about a remote
  attempt for `lost_after`, the attempt is failed with the reason `lost`,
  and the job's [retries](retries-and-backoff.md) apply like for any other
  failure. Any news about the attempt (a `started`, a heartbeat that
  lists it, a `finished`) stops its countdown, so an agent that comes
  back in time keeps its job.
- **Logs.** A remote job's log lives on the agent's machine, so asking for
  it (API, UI or `pysched log`) fetches it *from the agent*. If that agent
  is offline, the API answers **503** rather than a stale or empty log.
- **Reload.** `pysched reload` applies a changed secrets file straight
  away (add an agent, rotate a secret). But a reload that changes
  `listen`, `broker`, `agent_grace` or `lost_after` (or turns agents on or
  off) is refused as needing a **restart**, because the broker and the
  timers are set up once at start.
- **Secrets stay secret.** An agent's secret never appears in the API, the
  web UI, the logs or the saved config history. The API shows only names,
  tags, whether each agent is online, and what it is running.

## See It

`GET /api/agents`, the `/agents` page in the UI and `pysched agents` list
who is online, how busy each agent is and when it last spoke.

The repository has a demo that starts a scheduler and two agents in one
process, runs a pinned job and a tagged job, and prints which agent ran
each:

```text
uv run python examples/agents_demo.py
# build-app ran on build-01
# train-model ran on gpu-01
```

Here is the same idea without any processes: asking which agent a job
asks for.

```python
import tempfile
from pathlib import Path

from pyscheduler.config import load_config

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    secrets = root / "agents.yaml"
    secrets.write_text("- {name: gpu-01, secret: " + "g" * 40 + "}\n")
    secrets.chmod(0o600)
    path = root / "pysched.yaml"
    path.write_text(
        'agents: {listen: "127.0.0.1:8471", secrets_file: agents.yaml}\n'
        "jobs:\n"
        "  train:\n"
        "    command: ./train.sh\n"
        "    schedule: manual\n"
        "    agent_tags: [gpu]\n"
    )
    config = load_config(path, env={})

print(sorted(config.jobs["train"].agent_tags))  # ['gpu']
print(config.jobs["train"].agent)  # None
```
