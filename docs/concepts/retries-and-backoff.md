# Retries and Backoff (Knock Again, but Wait a Bit Longer Each Time)

## Knocking on a Friend's Door

You knock on a friend's door and nobody answers. You do not give up at
once: maybe they are on the stairs. You wait a minute and knock again.
Still nothing? This time you wait two minutes, then four. After a few
tries you go home and tell someone.

A job that fails is treated the same way. Its `retries` say how many
more knocks it gets, and how long to wait between them.

```yaml
jobs:
  fetch-prices:
    command: ./fetch.sh
    schedule: "0 7 * * mon-fri"
    retries: {count: 2, delay: 1m, backoff: 2, max_delay: 10m}
```

- `count`: how many retries after the first try (default 0: no retries).
- `delay`: the wait before the first retry (default `1m`).
- `backoff`: how much longer each wait is than the one before (default `2`).
- `max_delay`: the longest any wait can be (default `10m`).

## The Waits Grow

Retry number `n` waits `delay × backoff^(n-1)`, but never more than
`max_delay`. With the settings above:

| Try        | What happens                          |
|------------|---------------------------------------|
| attempt 1  | fails                                 |
| wait 1m    | the run is `retry_wait`               |
| attempt 2  | fails                                 |
| wait 2m    | the run is `retry_wait` again         |
| attempt 3  | the last chance: success or `failed`  |

Waiting longer each time is called **backoff**. If something is
overloaded, knocking faster only makes it worse; backing off gives it
room to recover.

## What Counts as a Failure?

An attempt fails when the command exits with a non-zero code, is killed
by a signal, runs past its `timeout`, or cannot be started at all (for
example, the program does not exist). Each of these is retried while
tries remain. The run's reason says what went wrong: `exit 3`,
`signal 9`, `timeout` or `start error: No such file ...`.

Two endings are never retried:

- **success**, of course;
- **killed by an operator**: if a person stopped it on purpose, the
  scheduler does not start it again behind their back.

## Timeouts: Asking Nicely, Then Not

A `timeout` is a stopwatch on each attempt. When it rings, the
scheduler asks the job to stop (SIGTERM). If the job ignores that, it
is stopped for good (SIGKILL) after `kill_grace` (10 seconds unless the
`scheduler:` section says otherwise). The attempt's outcome is
`timeout`, and it may be retried like any other failure.

## Reruns Reset the Count

The run remembers how many attempts it had when an operator last
reran it (`rerun`). The tries that count against `count` are only the
attempts since then, so a rerun gets a fresh set of knocks.

## Try It

```python
from datetime import timedelta

from pyscheduler.config.model import RetryPolicy

policy = RetryPolicy(count=4, delay=timedelta(minutes=1), backoff=2, max_delay=timedelta(minutes=5))
for n in range(1, 5):
    print(n, policy.delay_for(n))
# 1 0:01:00
# 2 0:02:00
# 3 0:04:00
# 4 0:05:00   <- capped by max_delay
```
