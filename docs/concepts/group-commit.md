# Group Commit (One Trip to the Post Box for Many Letters)

## The Post Box

Imagine a classroom where everyone writes a letter home. If each child
walks to the post box on their own, that is thirty walks. Much better: one
child is the **clerk**. Everyone drops their letter in a tray, and the
clerk carries the whole tray to the post box in one trip. A letter dropped
in the tray while the clerk is out simply goes on the next trip.

Nobody leaves the classroom thinking "my letter is posted" until the clerk
comes back and says so. That is the important bit.

## Write First, Then Act

PyScheduler's engine writes every change to a run in its notebook, the
**store**, *before* it acts on it. It saves "this run is now running"
and waits until that is safely on the disk, and only then starts the
process. If the computer loses power a moment later, the notebook never
says "not started" about a job that really did start. This is called
**write-ahead**.

Making something safe on the disk means an `fsync`: asking the disk to
really, truly write it down, which is slow (like the walk to the post
box). With hundreds of runs changing at once, one `fsync` each would be
far too slow.

## The Clerk: `StoreWriter`

`StoreWriter` is the clerk:

1. `await writer.save(batch)` drops a `Batch` of changes (runs, attempts,
   events, engine notes) in the tray and waits.
2. One writer task takes *everything* in the tray, merges it into one
   batch (if two batches change the same run, the later one wins), and
   commits it as one PyDB `Transaction`: one journal entry, one `fsync`.
3. The commit happens on a helper thread (`run_in_thread`), so the event
   loop keeps serving everyone else while the disk is busy.
4. When the commit is done, every waiting `save` returns. If it failed,
   every waiter in that trip gets the same error, and the next trip
   starts fresh.

When the scheduler is quiet, one change goes on its own, straight away.
When it is busy, many changes share a trip. `writer.commits` counts the
trips.

```python
from pathlib import Path
from tempfile import mkdtemp

import pynet.loop

from pyscheduler.store import Batch, Store, StoreWriter

store = Store.open(Path(mkdtemp()) / "state")
writer = StoreWriter(store)


async def main() -> None:
    await writer.start()
    saves = [pynet.loop.spawn(writer.save(Batch(engine={f"note{n}": "hi"}))) for n in range(50)]
    for save in saves:
        await save
    await writer.close()  # posts anything still in the tray, then stops


pynet.loop.run(main())
print(writer.commits)  # 1 -- fifty letters, one trip
store.close()
```

## Finding Things Fast

The store keeps two address books in memory: from a run id to where its
row lives in PyDB, and from `(run id, attempt)` to where the attempt
lives. So saving a run never means reading the whole table to find it.
The address books change only after a commit succeeds, so a failed trip
leaves them exactly right. Questions such as "the runs of this job" use
PyDB indexes.

PyDB has no "empty" (NULL) value yet, so a value that might be missing,
such as an attempt's end time while it is still running, is kept as text,
where empty text means "not known yet".
