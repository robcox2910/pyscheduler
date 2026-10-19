# Live Updates (The Radio With Numbered Messages)

## The Radio in the Dining Hall

During lunch the head teacher reads out news over a radio: "Lesson 3B
has moved to the lab", "The bus is late". Each item is **numbered**.
Suppose your radio crackles out for a minute and comes back at item 41.
You say "I last heard number 38", and a helper reads out 39, 40 and 41
from a clipboard. If you were gone so long that the clipboard no longer
reaches back to 38, nobody can fill the gap, so the helper says one
thing only: **start again**. You walk back in and read the noticeboard
afresh.

That is how the board stays up to date without you pressing refresh.

## Server-Sent Events

The browser opens one long-lived request to `/events`, and the server
sends a line of news whenever something happens (this is called
**Server-Sent Events**, SSE). The news has a type:

- `run`: a run changed state (`run_id`, `job`, `run_date`, `state`);
- `attempt`: an attempt started or ended;
- `reload`: a config reload put a new version in force.

The page does not build rows from this news. It asks the server for
the new HTML of just that row (`/ui/fragments/board-row/<run_id>`) and
swaps it in, so all drawing lives in one place, in Python.

## Why Ids and Replay

Pyweb's `EventHub` just shouts; it does not number anything and it
cannot replay. A browser that loses its connection (a laptop lid, a bad
wifi) would silently miss changes and show a board that lies. So
`ui/live.py` adds:

- a **rising id** on every event, like `a1b2c3d4-41`: the first part
  names this run of the scheduler (its *boot*), the second counts up;
- a **ring buffer** of the last 1000 events (`LIVE_BUFFER`);
- **replay**: a reconnecting browser sends `Last-Event-ID`, and gets
  every buffered event after it;
- **reset**: if the id is from another boot (the scheduler restarted),
  is garbage, or fell off the buffer, the server sends a single `reset`
  event and the page reloads itself.

A page's very first connection cannot send `Last-Event-ID`, so each
page carries the newest id at the moment it was drawn and connects to
`/events?after=<id>`; news that came out while the page was on its way is
replayed too.

Events are published only after the change is safely in the store, so a
page never hears about something a crash could take back.

## Tailing a Log

`GET /events/log/<run_id>?stream=stdout|stderr&attempt=` is the same
idea for a log file: it sends the last 200 lines, then every second the
new whole lines (at most 64 KiB per tick), and an `end` event a moment
after the attempt has finished. If the file shrinks (it was rotated)
it starts again from the top with a `reset` line.

## Try It

```python
import pynet.loop

from pyscheduler.ui.live import LiveFeed

feed = LiveFeed(buffer=3, boot="demo")
for n in range(5):
    feed.publish("run", {"run_id": f"job.2026-10-19.{n}", "state": "succeeded"})
print(feed.last_id)  # demo-5


async def listen(last_heard: str, count: int) -> list[tuple[str | None, str | None]]:
    stream = feed.stream(last_heard)
    heard = []
    for _ in range(count):
        event = await anext(stream.source)
        heard.append((event.id, event.event))
    return heard


# only the last 3 are kept (ids 3, 4, 5), so "heard 3" gets 4 and 5 ...
print(pynet.loop.run(listen("demo-3", 2)))  # [('demo-4', 'run'), ('demo-5', 'run')]
# ... but "heard 1" is too old to fill the gap: one reset
print(pynet.loop.run(listen("demo-1", 1))[0])  # ('demo-5', 'reset')
# ... and so is an id from another boot
print(pynet.loop.run(listen("other-9", 1))[0])  # ('demo-5', 'reset')
```
