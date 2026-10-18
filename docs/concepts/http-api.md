# The HTTP API (The Front Desk With Wristbands)

## The Front Desk

A big festival has a front desk. Visitors never wander backstage to
move the lights themselves: they come to the desk, show their
**wristband**, and ask. A green band lets you look at the timetable (a
**viewer**). A gold band also lets you ask the crew to do things, like
start a show early or stop one (an **operator**). The steward does not
keep a copy of your band, only its **fingerprint**, so a stolen guest
list opens no gates.

PyScheduler's HTTP API is that front desk. The `pysched` command, your
scripts and (later) a web page all talk to it in JSON, and it talks to
the engine for them.

## Wristbands (Bearer Tokens)

Callers send a header:

```text
Authorization: Bearer <token>
```

The `api_tokens_file` lists each band's name, the SHA-256 fingerprint
of its token (64 lowercase hex digits), and its role:

```yaml
- {name: ci, token_sha256: 3f5a...e1, role: operator}
- {name: dashboard, token_sha256: 9b1c...07, role: viewer}
```

- `GET` requests need a viewer or an operator; anything else needs an
  operator.
- A missing or unknown token gets `401 {"error": "unauthorised"}`
  (with `WWW-Authenticate: Bearer`); a viewer trying an action gets
  `403 {"error": "forbidden"}`.
- `GET /api/health` needs no token at all, so a load balancer can ask.
- The token's **name** is recorded as the actor of every action, so the
  logbook says *who* pressed the button.
- A malformed tokens file stops the server at start-up with a message
  naming the file and line.
- `POST /api/reload` and `SIGHUP` read the tokens file again too, so a
  token you delete from it stops working straight away, with no
  restart. If the new tokens file is broken, the whole reload fails
  (the old config and the old tokens stay) and the problem is reported.
  A reload cannot switch between development mode and tokens: that
  needs a restart.
- The `pysched` command reads its token from `PYSCHED_TOKEN`. Prefer
  that over `--token`: anything on a command line shows up in `ps` for
  every user on the machine.

**Every POST is JSON.** In every mode, a `POST` must carry
`Content-Type: application/json` (a `; charset=utf-8` is fine), even
when it has no body; anything else gets
`415 {"error": "use Content-Type: application/json"}`. A web page on
another site can make your browser send a plain form or a `text/plain`
post, but not a JSON one without asking the server first, so this
stops a page you happen to visit from pressing buttons. The `pysched`
command always sends the header.

**Development mode.** With no tokens file, everyone is `dev` with the
operator role. The server then refuses to listen anywhere but a
loopback address such as `127.0.0.1`, so other machines cannot reach
it. That is *not* the same as private: every program on *this* machine
can, including your web browser while it shows some other site. So in
development mode a request whose `Host` header is not `localhost`,
`127.x.x.x` or `[::1]` (with an optional port) gets
`403 {"error": "forbidden host"}`. That stops a page that points its
own domain name at `127.0.0.1` (a trick called *DNS rebinding*) from
reading or steering the scheduler. Use tokens on any machine you share.

## What You Can Ask

| Route | What it does |
| --- | --- |
| `GET /api/health` | `{"status": "ok", "timezone": "Europe/London", "today": "2026-10-19"}` (the scheduler's time zone and today's date there), or 503 `{"status": "failed"}` when the engine has failed stop |
| `GET /api/jobs` | every job: owner, tags, criticality, schedules as text, next firing |
| `GET /api/jobs/{name}` | the same plus its `definition`, its next 5 firings and its 20 latest runs |
| `GET /api/runs?run_date=&job=&state=&limit=` | runs, newest first (`limit` 1-1000, default 100) |
| `GET /api/runs/{id}` | one run with its `attempts` and `events` |
| `GET /api/runs/{id}/log?stream=stdout&attempt=&tail=200` | the last lines of an attempt's log (`tail` up to 5000) |
| `GET /api/forecast?from=&to=` | the predicted runs of a window (default the next 24 hours, at most 31 days) |
| `POST /api/jobs/{name}/trigger` | a manual run; body `{"run_date": "YYYY-MM-DD"}` optional |
| `POST /api/jobs/{name}/backfill` | body `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}` |
| `POST /api/runs/{id}/{action}` | `rerun` (body `{"downstream": true}` optional), `hold`, `release`, `kill`, `force-start`, `skip`, `mark-success`, `mark-failed` |
| `POST /api/reload` | reload the config files; 400 with `problems` if they are broken (the old config stays) |
| `GET /api/config/validate` | check the files on disk without loading them |

Run ids are one path segment: `/api/runs/extract-trades.2026-10-17.1/rerun`.

## The Shapes

- Instants are ISO 8601 in UTC with a `Z`: `"2026-10-19T18:30:00Z"`.
- Dates are `"2026-10-19"`; enums are their values (`"succeeded"`).
- Durations are seconds, in fields ending `_seconds`.
- A run in `GET /api/runs/{id}` has `duration_seconds` from its latest
  attempt (`null` while that attempt runs); run lists leave it out.
- A job's `definition` shows its environment variable **names** only
  (`"***"` for each value), because values may be secrets.
- Any value the config got from `${NAME}` is put back as `${NAME}` in
  every string of a job summary or definition (command, cwd,
  description, alerts, ...), longest values first. Values shorter than
  4 characters are left alone (hiding them would mangle ordinary text),
  so do not keep secrets that short.
- A job's `alerts` settings (webhooks, addresses) are shown to
  operators only; viewers see `"***"`.
- Paths (log files, working directories, config file names) are shown
  to viewers too: the API is an operator tool for a trusted network,
  and knowing where a log lives is part of reading it.
- When the engine has failed stop, actions answer 503 `"scheduler
  failed; see the scheduler log"`; the cause is only in the log.
- Errors are always `{"error": "..."}` with 400, 404, 409 or 503. An
  unexpected bug answers 500 `{"error": "internal error"}` and the
  details go to the scheduler's log, never to the caller.

## Only the Safe Truth

The API shows the run book's **published** view: states already safely
written to the store, so it never shows a state that a crash could take
back. Runs older than the in-memory window (`memory_days` plus the dependencies' reach) are read from
the store. Every store read and log file read happens on a helper
thread, so one slow disk never holds up the event loop.

## Try It

Make a token and its fingerprint for the tokens file:

```python
import hashlib
import secrets

token = secrets.token_urlsafe(32)  # give this to the caller
print(token)
print(hashlib.sha256(token.encode()).hexdigest())  # put this in the tokens file
```

Then drive an API in memory with pyweb's `TestClient` (no network needed):

```python
from datetime import UTC, datetime
from pathlib import Path
import tempfile

import pynet.loop
from pyweb.testing import TestClient

from pyscheduler.api import build_app, config_actions
from pyscheduler.config import load_config
from pyscheduler.engine.clock import FakeClock
from pyscheduler.engine.core import Engine
from pyscheduler.store import Store
from pyscheduler.testing import FakeLauncher

home = Path(tempfile.mkdtemp())
path = home / "pysched.yaml"
path.write_text(f"""
scheduler:
  state_dir: {home / "state"}
  log_dir: {home / "logs"}
jobs:
  bake:
    command: ./bake.sh
    schedule: manual
""")

config = load_config(path)
clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
engine = Engine(
    config, Store.open(home / "state", sync=False), clock=clock, launcher=FakeLauncher(clock)
)
reload, validate = config_actions(engine, path)
client = TestClient(build_app(engine, tokens=None, reload=reload, validate=validate))
local = {"Host": "127.0.0.1:8470"}  # development mode answers loopback hosts only


async def visit() -> None:
    await engine.start()
    made = await client.post("/api/jobs/bake/trigger", json={}, headers=local)  # no token
    run_id = made.json()["run"]["run_id"]
    print(run_id)  # bake.2026-10-19.1
    detail = (await client.get(f"/api/runs/{run_id}", headers=local)).json()
    print([event["actor"] for event in detail["events"]])  # ['scheduler', 'dev', 'scheduler']
    await engine.stop()


pynet.loop.run(visit())
```
