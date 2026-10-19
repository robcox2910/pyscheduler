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
| `/admin` | Config versions (with a diff against the version before), their sources, the dead-letter tray, and the reload and replay buttons. Admins only. |

The page builds its HTML on the **server**, in Python, and escapes every
piece of text it did not write itself. A job named `<script>` shows up
as the text `<script>`, never as a script. The small bit of JavaScript
only adds behaviour. It is a handful of modules: `app.js` is the one
entry point the page loads, and it imports `common.js` (the CSRF token,
posts, the shared live feed, and what to do when the session is gone or
the scheduler is restarting), `board.js`, `actions.js`, `admin.js` and
`log.js`. Together they post button clicks, tail logs and swap in rows
the server has drawn. They never build HTML from text.

## Who May Do What

Three roles, each including the one before:

- **viewer**: look at every page;
- **operator**: also press the action buttons on a run (rerun, hold,
  release, kill, skip and friends) and trigger jobs;
- **admin**: also open the admin pages: config versions, their sources
  and diffs, and the dead-letter tray, with its reload and replay
  buttons.

Reloading and replaying are **operator** actions in the JSON API
(`POST /api/reload`, `POST /api/alerts/<id>/replay`), as they were
before the web UI: operator tokens and `pysched reload` keep working.
The admin role adds the admin *pages*, the place in the browser where
those buttons live.

The login page checks a name and password against the `users_file`
(make a line for it with `pysched hash-password`). Behind the scenes
there is **one access layer** for the pages and the JSON API alike:
a bearer token and a login session both end up as the same "who is
this, and what role?". See [The HTTP API](http-api.md) for the modes and
the CSRF header.

## Sessions, Sign-Out and Removing Someone

A login lasts **8 hours**. The session cookie is *signed*, not stored:
the scheduler keeps no list of sessions, so it can check a cookie but
cannot cross one out. That has two sides:

- **Signing out does not revoke a stolen cookie.** It clears the cookie
  in *your* browser; a copy taken earlier still works until its 8 hours
  are up.
- **Removing the user does.** Take them out of the `users_file` and
  reload (`POST /api/reload`, `pysched reload` or `SIGHUP`): their next
  request is refused, and every open live stream and log tail is ended
  at the reload, so even a tab left open stops hearing news. Everybody
  else's browser reconnects at once and misses nothing. The same goes
  for an API token removed from the tokens file.

## HTTPS and `secure_cookies`

The scheduler speaks plain HTTP. By default the session cookie is marked
`Secure` unless the scheduler listens on a loopback address, and a
browser never sends a `Secure` cookie back over plain HTTP. So when the
UI is opened on another machine, put an **HTTPS reverse proxy** in front
of it. On a trusted network without one, turn it off:

```yaml
scheduler:
  listen: 0.0.0.0:8470
  users_file: users.yaml
  secure_cookies: false   # plain HTTP on a trusted network only
```

When the cookie is `Secure` and the listen address is not loopback, the
scheduler logs one warning at start-up saying exactly this.

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
The seed writes a `seeded` marker only once every run has finished, so a
seed that failed or was stopped with Ctrl-C ("seed interrupted") is simply
done again next time. Seeding runs real child processes, so
`seed_demo` and `serve_demo` must be called on the main thread.

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
