# Concurrency (One Waiter, Two Hundred Tables)

## The Busy Restaurant

A good waiter can look after a whole restaurant on their own, as long as
they never stand still at one table. Take an order, move on. Bring a
plate, move on. The moment the waiter stops to fold napkins for five
minutes, every other table is left waving.

PyScheduler's engine is that waiter (see
[The Scheduler Engine](scheduler.md)): one **event loop** looks after every
running job. That only works if nothing the loop does takes long. So we
measure it.

## The Kitchen Timer Test

How can you tell whether the waiter is ever stuck? Set a kitchen timer to
ring every 10 milliseconds and, each time it rings, write down **how
late** it was. A free waiter answers almost at once. A waiter folding
napkins answers late, and the lateness says for how long they were stuck.

That is the **loop-lag probe**. Here it is catching a blocking call
(`time.sleep`, the napkin folding) and then not catching the same work
done on a helper thread (`run_in_thread`):

```python
import time

import pynet.loop


async def worst_lateness(job: str) -> float:
    """Tick every 10 ms while some slow work happens; return the worst lateness."""
    loop = pynet.loop.get_running_loop()
    worst = 0.0
    due = time.monotonic() + 0.01

    def tick() -> None:
        nonlocal worst, due
        worst = max(worst, time.monotonic() - due)
        due = time.monotonic() + 0.01
        loop.call_later(0.01, tick)

    loop.call_later(0.01, tick)
    await pynet.loop.sleep(0.05)
    if job == "on the loop":
        time.sleep(0.2)  # blocks the waiter
    else:
        await pynet.loop.run_in_thread(time.sleep, 0.2)  # a helper does it
    await pynet.loop.sleep(0.05)
    return worst


for job in ("on the loop", "on a helper thread"):
    late = pynet.loop.run(worst_lateness(job))
    print(f"{job}: {late * 1000:.0f} ms late")
# on the loop: 200 ms late
# on a helper thread: 0 ms late (or a millisecond or two)
```

## The Proof: Two Hundred Jobs at Once

`tests/test_concurrency.py` runs the real thing: a real clock, a real
store on disk and a real minder process for every attempt. Two hundred
jobs of `sleep 0.2` are triggered at the same moment with
`max_running: 200`, and the test checks that:

- all 200 succeed in under **6 seconds**;
- never more than 200 are running at once;
- the kitchen timer is never more than **50 ms** late.

A second test runs 50 jobs with `max_running: 8` and watches every state
change: never more than 8 are running.

## What the Probe Found

The first run of the probe was humbling: the loop was up to **1.8
seconds** late. These were the napkins, and where they went:

| Stuck on | Fix |
|----------|-----|
| Starting each minder (`fork` and `exec`, about 5 ms each) on the loop | A launch thread starts them; the loop only hears "it's running" |
| Writing and `fsync`ing each minder's `.pid` file on the loop | Done on the launch thread too |
| Reading each status note when an attempt ends | Read on a helper thread |
| Starting 200 queued runs in one go | The pump starts at most 16 per turn, then lets others have a go |
| 200 Python minders starting at once, each importing `argparse`, `subprocess`, `json`... | The minder travels light (see [The Minder](the-minder.md)): about a third less work each |

One surprise: **more launch threads made it worse**. Python's `fork`
keeps hold of the interpreter's lock (the GIL) while it copies the
scheduler, so four threads forking together kept the loop's own thread
waiting for its turn. With one launch thread, the loop always gets a turn
between two forks.

After the fixes, on a six-core laptop, the 200 jobs finish in about 2.5
seconds and the timer is never more than about 30 to 45 ms late (the
higher figures with test coverage switched on, which slows Python down).

## Why Not Just Raise the Limit?

Because the limit is the point. A probe that is allowed to be a second
late would not notice a second-long stall, and a scheduler that stalls
for a second is a scheduler whose timeouts, kills and API answers are a
second late. When the probe fails, find what blocked the loop and move it
off.
