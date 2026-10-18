# Notifiers (The Ways to Reach Someone)

## The School Office Has Many Ways to Get a Message Out

When something goes wrong at school, the office can pin a note on the
notice board, phone a helper, send a text, or write an email. Each is a
different **way to reach someone**, and the office does not care which
you pick: it just hands over the message.

A **notifier** is one of those ways. You name each one in the `notifiers:`
section of the config, give it a `type`, and then point routes
or a job's `alerts: {notify: [...]}` at its name. Every notifier also
accepts two shared settings:

- `timeout:` how long one delivery may take (default `10s`).
- `retries:` `{count: 5, delay: 10s, max_delay: 10m}`, how often to try
  again when a delivery fails, waiting longer each time.

Everything else you write under a notifier is an *option* for its type.
An option that is missing or not recognised is reported when the config
loads, at the notifier's line, long before an alert needs it.

## The Six Types

| Type | What it does | Options |
| --- | --- | --- |
| `log` | Appends the alert as one JSON line to a file. | `path` (required) |
| `command` | Runs a program and gives it the alert JSON on stdin. | `command` (required) |
| `webhook` | Posts the alert JSON to a web address. | `url` |
| `slack` | Posts a message to a Slack incoming webhook. | `webhook_url` |
| `telegram` | Sends a message through a Telegram bot. | `bot_token`, `chat_id` |
| `email` | Sends an email over SMTP. | `host`, `to`, ... |

The `webhook`, `slack`, `telegram` and `email` types talk over the
network; their details are covered further down.

### log: the notice board

```yaml
notifiers:
  team-log: {type: log, path: logs/alerts.log}
```

A relative `path` is measured from the folder holding the config file, so
the same config works wherever you run the scheduler. Missing folders are
created.

### command: the phone call to a helper

```yaml
notifiers:
  page-me:
    type: command
    command: "/usr/local/bin/page-oncall"   # run with /bin/sh -c
    timeout: 5s
```

`command` is either text (run by `/bin/sh -c`) or a list of arguments
(run directly, no shell). The alert JSON arrives on the program's stdin.
If it exits with anything but 0 the delivery fails, and the last line it
wrote to stderr is kept as the reason. If it is still running when the
`timeout` ends, it and everything it started are stopped.

## Trying a Notifier

You can build a notifier by hand and send it a sample alert:

```python
import json
import tempfile
from pathlib import Path

import pynet.loop

from pyscheduler.config import load_config
from pyscheduler.notifiers import build_notifier

folder = Path(tempfile.mkdtemp())
config_file = folder / "scheduler.yaml"
config_file.write_text(
    """
notifiers:
  team-log: {type: log, path: logs/alerts.log}
jobs:
  hello: {command: "true", schedule: manual}
""",
    encoding="utf-8",
)
config = load_config(config_file, env={})
notifier = build_notifier(config.notifiers["team-log"], config.settings)

pynet.loop.run(notifier.send({"kind": "failed", "job": "hello", "message": "hello failed"}))

line = (folder / "logs" / "alerts.log").read_text(encoding="utf-8").splitlines()[0]
print(json.loads(line)["message"])
```

## Failing Safely

A notifier that cannot deliver raises a `NotifyError`. The scheduler
writes the alert down *before* any notifier is called, so a failed
delivery is retried later and never loses the alert.

## Plug-in Types

Another package can add its own type by registering an entry point in the
`pyscheduler.notifiers` group. The entry point's name is the `type`, and
it loads to a function `(definition, settings) -> notifier` that raises
`ValueError` with a friendly message for bad options.
