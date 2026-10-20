# Heartbeats (The Walkie-Talkie Check-In)

## "Still Here!"

A delivery driver is out of sight all day. How does the shop know the
driver has not crashed the van into a hedge? The shop gives each driver a
walkie-talkie and a rule: say "still here" every ten seconds. If the
radio goes quiet for a minute, the shop writes "driver offline" on the
whiteboard. If it stays quiet for ten minutes, the shop gives up on the
parcels that driver was carrying.

That is a **heartbeat**. A running `pysched-agent` sends a signed
`heartbeat` message every 10 seconds (and a `hello` when it first
connects). Each one says which attempts the agent is running right now.

!!! warning "The broker has no door guard"
    Heartbeats travel over the pymq broker, which has **no authentication
    and no encryption**. The messages are sealed (see
    [Signed Messages](signed-messages.md)), so nobody can fake a heartbeat
    without the secret, but anyone who can reach the port can read them.
    Use loopback, a trusted network or a tunnel.

## Two Timers, Two Meanings

| Setting | Default | What happens when it runs out |
| --- | --- | --- |
| `agent_grace` | `60s` | The agent is marked **offline**. No new work is sent to it, and asking it for a log gives a 503. |
| `lost_after` | `10m` | An attempt that has gone unheard is **lost**: it is failed with the reason `lost`, and retries apply. |

Both are set in the [`agents:` section](remote-agents.md) and both need a
restart to change. Any message from the agent re-arms `agent_grace`; any
news *about an attempt* stops that attempt's `lost_after` countdown. When
an agent comes back and reports an attempt it is still running, the job
simply carries on. When it comes back and does *not* list an attempt
(because it restarted and the job is gone), the countdown for that attempt
starts then.

An order that the agent has not yet confirmed (`started`) is posted again
every `agent_grace`, so a dropped letter does not strand a job. The agent
writes an order in its logbook before saying it got it, so an order
delivered twice still runs once.

## Try It

The scheduler's sign-in sheet is the `AgentBook`. Here a fake clock
shows an agent going offline after `agent_grace` of silence and coming
back on its next heartbeat.

```python
from datetime import UTC, datetime, timedelta

import pynet.loop

from pyscheduler.agents.book import AgentBook
from pyscheduler.engine.clock import FakeClock


async def main() -> None:
    """Let an agent go quiet, then speak again."""
    clock = FakeClock(datetime(2026, 10, 20, 9, tzinfo=UTC))
    book = AgentBook(clock=clock, grace=timedelta(seconds=60))
    book.heard("gpu-01", tags={"gpu"}, max_running=2, running=[])
    print("after hello:", book.states()[0].online)
    await clock.advance(timedelta(seconds=30))
    print("30s quiet:", book.states()[0].online)
    await clock.advance(timedelta(seconds=31))
    print("61s quiet:", book.states()[0].online)
    book.heard("gpu-01", running=[])
    print("heartbeat:", book.states()[0].online)


pynet.loop.run(main())
# after hello: True
# 30s quiet: True
# 61s quiet: False
# heartbeat: True
```
