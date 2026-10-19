# The Web UI (The School Noticeboard)

## The Noticeboard in the Hall

Every school has a noticeboard in the main hall. Without asking anyone
you can see which lessons are on, which have been cancelled and which
one is in trouble (that one has a red card pinned on top). Teachers can
pin new notes up; pupils can only read.

PyScheduler's **web UI** is that noticeboard. Open it in a browser, log
in, and you see today's runs as rows with traffic-light badges. Runs
that failed *and* are marked `critical` are pinned to the top, like the
red card. Everything else follows in the order it was scheduled.

## The Pages

| Page | What it shows |
| --- | --- |
| `/` | The **board**: one row per run for a date, with state, times, duration, "vs typical", attempt and tags. Filter by job name, state, tag or criticality; step to the day before or after. |
| `/jobs/<name>` | One job: its definition, its next firings, its recent runs and a sparkline of durations. |
| `/runs/<id>` | One run: attempts, events, alerts and their deliveries, and a **live log tail** with tabs for stdout and stderr. |
| `/graph` | The dependency graph for a date, coloured by state. `?focus=job` lights up everything upstream and downstream; `?tag=` keeps one tag. |
| `/timeline` | When each job really ran, and when it is forecast to. See [Timelines](timelines.md). |
| `/forecast` | The forecast as a table: what will start, and roughly when. |
| `/admin` | Config versions (with a diff against the version before) and the dead-letter tray. Admins only. |

The page builds its HTML on the **server**, in Python, and escapes every
piece of text it did not write itself. A job named `<script>` shows up
as the text `<script>`, never as a script. The small bit of JavaScript
(`app.js`) only adds behaviour: it posts button clicks, tails logs and
swaps in rows the server has drawn. It never builds HTML from text.

## Who May Do What

Three roles, each including the one before:

- **viewer**: look at every page;
- **operator**: also press the action buttons on a run (rerun, hold,
  release, kill, skip and friends) and trigger jobs;
- **admin**: also open `/admin`, reload the config and replay dead
  alerts.

The login page checks a name and password against the `users_file`
(make a line for it with `pysched hash-password`). Behind the scenes
there is **one access layer** for the pages and the JSON API alike:
a bearer token and a login session both end up as the same "who is
this, and what role?". See [The HTTP API](http-api.md) for the modes and
the CSRF header.

## Try the Demo

The repository has a ready-stocked demo: about twelve jobs (two chains,
a file watcher, a flaky job that fails once and is retried, and a
critical job that always fails), run for three days with real quick
commands.

```text
uv run python examples/ui_demo.py        # seeds, then serves until Ctrl-C
```

It prints a loopback address and the login (`admin`, password `demo`).
`--seed-only` seeds and exits, and `--seconds 60` serves for a minute.

Without starting anything, the building blocks can be tried directly.
This is the helper every page is built with, showing the escaping:

```python
from pyscheduler.ui.html import h

row = h("tr", None, h("td", None, "<script>alert(1)</script>"), h("td", {"class": "ok"}, "ok"))
print(row)
# <tr><td>&lt;script&gt;alert(1)&lt;/script&gt;</td><td class="ok">ok</td></tr>
assert "<script>" not in str(row)
```

And the seed itself, which takes a few seconds, so it is not run by the
docs tests:

<!-- no-run -->
```python
from pathlib import Path

from pyscheduler.demo import seed_demo, serve_demo

seed_demo(Path("ui-demo"))  # writes the config, the login and the runs
serve_demo(Path("ui-demo"), seconds=60)  # prints the address; stops after a minute
```
