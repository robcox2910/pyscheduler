# The Service and the CLI (Opening the Shop and the Remote Control)

## Opening the Shop

A shopkeeper opens up the same way every morning, in the same order:

1. read the **delivery note** (is everything that should be here, here?);
2. unlock the **stockroom**;
3. wake the **staff**;
4. and only then turn the sign on the door to **OPEN**.

Closing is the same list backwards: turn the sign round, let the
customers already inside finish, then send the staff home.

`pysched serve` opens PyScheduler like that:

1. it reads the **config** (and stops at once if it has errors, before
   touching anything);
2. it opens the **store** (the notebook of runs in `state_dir`);
3. it starts the **engine** (recovery, catch-up, then the firing task),
   and remembers the config version if the files changed;
4. it opens the **HTTP API** on `listen` (default `127.0.0.1:8470`).

The door is actually unlocked a little early: the port is taken right
after the store opens, so a busy port stops `serve` before any job is
started. Until the engine is ready, every request gets
`503 scheduler is starting`.

Closing (Ctrl-C or `SIGTERM`) turns the sign round: the API stops taking
new requests, requests already inside get 5 seconds to finish, and the
engine stops. Jobs that are running are **not** killed: their minders
keep babysitting them, and the next `serve` adopts them again.

`SIGHUP` (`kill -HUP <pid>`) is like a new delivery note arriving while
the shop is open: the scheduler re-reads its config files and swaps them
in if they are valid. If they are not, it writes the problems in its log
and carries on with the old config.

!!! warning "Development mode is for your own computer only"
    With no `api_tokens_file`, everyone who can reach the API is an
    operator. That is why `serve` refuses to start in that mode unless
    `listen` is a loopback address (`127.0.0.1`, `::1` or `localhost`).
    Use `pysched hash-token` to make a token for the tokens file.

## The Remote Control

`pysched` is also the remote control. Some buttons work with the
scheduler switched off, because they only read files on your computer:

- `pysched validate FILE`: check the config (errors on stdout, one
  `file:line: message` each; warnings on stderr);
- `pysched forecast FILE --from 2026-10-19 --to 2026-10-20`: which jobs
  should start when;
- `pysched hash-token`: a new random token and its SHA-256.

The rest send a request to the API:

- `status [--date D]`, `runs`, `show RUN`, `log RUN [--stderr] [--tail N]`;
- `trigger JOB [--run-date D]`, `backfill JOB --from D --to D`, `reload`;
- `rerun`, `hold`, `release`, `kill`, `force-start`, `skip`,
  `mark-success`, `mark-failed` (each takes a run id).

Point it at the scheduler with `--url` (or `PYSCHED_URL`) and prove who
you are with `PYSCHED_TOKEN` (or `--token`). Prefer the environment
variable: a `--token` on the command line shows up in `ps` for every
user of the machine. It answers with an **exit
code**: `0` it worked, `1` the scheduler said no (or could not be
reached, or for `serve`: the state folder is in use by another
scheduler), `2` you typed something wrong or the config has errors.
`status` shows today's runs, where "today" is the scheduler's date in
its own time zone. `trigger` without `--run-date` (alias `--date`)
picks the run date the job's own `run_date` rule gives today: for
`same_day`, today in the job's time zone (or the latest business day
before it, if today is not one); for `previous_business_day`, the
business day before today.

## Try It

```console
$ cat > hello.yaml <<'EOF'
scheduler:
  state_dir: ./state
  log_dir: ./logs
jobs:
  hello:
    command: "echo hello"
    schedule: manual
  after:
    command: "sleep 1"
    depends_on: [hello]
EOF
$ pysched validate hello.yaml
ok: hello.yaml has 2 jobs
$ pysched serve hello.yaml &
$ pysched trigger hello
hello.2026-10-19.1: queued
$ pysched status
JOB    RUN                 STATE      ATTEMPT  STARTED                      DURATION
after  after.2026-10-19.1  running    1        2026-10-19T09:00:00.117000Z  0.4s+
hello  hello.2026-10-19.1  succeeded  1        2026-10-19T09:00:00.010000Z  0.1s
$ pysched log hello.2026-10-19.1
hello
$ kill %1
```

The same client works from Python, without an event loop:

```python
from pyscheduler.client import ApiClient, ApiError

client = ApiClient("http://127.0.0.1:8470", token=None)
try:
    print(client.health())
except ApiError as exc:
    print("no scheduler here:", exc.message)
```
