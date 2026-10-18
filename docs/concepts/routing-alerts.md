# Routing Alerts (The Post Office With a Pigeonhole per Notifier)

## Sorting the Post

Picture a small post office. Letters arrive with an address written on
the front, and the clerk sorts each one into a **pigeonhole**: one
pigeonhole per postman. Each postman empties only their own pigeonhole,
walks the round, and comes back. If nobody was home, the letter goes back
in the pigeonhole to try again later. A letter that fails too often goes
in the **dead letter** box, where someone can look at it and post it again.

The scheduler's alerts travel the same way:

- The **address** is the alert's *topic*, such as `alert.failed.critical.load`.
- The **sorting rules** are the `routes:` in the config.
- Each **notifier** has its own pigeonhole: a PyMQ work queue called
  `notify.<name>`.
- The **postman** is the dispatcher, which takes copies out of each
  queue and hands them to the notifier.

## Topics and the Two Wildcards

Every alert has a topic made of four words joined by dots:

```text
alert.<kind>.<criticality>.<job>
```

So a critical failure of the job `load` is `alert.failed.critical.load`,
and a reload that went wrong (no job) is `alert.reload_failed.high._scheduler`.

A route says which topics it wants with a pattern. Two wildcards help:

- `*` matches **exactly one** word: `alert.*.critical.load` is any kind of
  critical alert for `load`.
- `#` matches **any number** of words, even none: `alert.failed.#` is
  every failure, whatever its criticality or job.

```python
from pymq.broker.topics import topic_matches

topic = "alert.failed.critical.load"
print(topic_matches("alert.failed.#", topic))  # True: '#' takes the last two words
print(topic_matches("alert.*.critical.load", topic))  # True: '*' takes 'failed'
print(topic_matches("alert.*.critical", topic))  # False: '*' is exactly one word
print(topic_matches("alert.#", "alert"))  # True: '#' may take no words at all
```

An alert goes to every notifier of every route that matches, plus the
job's own `alerts: {notify: [...]}`, each notifier once.

```yaml
routes:
  - {match: "alert.*.critical.#", notify: [ops-slack]}
  - {match: "alert.failed.#", notify: [team-log]}
```

## Write It Down First

Before any postman sets off, the scheduler writes the alert and one
**delivery** per notifier into its store, each marked `pending`. Only then
does it put one copy in each notifier's pigeonhole. When the postman comes
back the delivery is updated to `sent`, or kept `pending` with the error,
or marked `dead`.

The broker keeps its pigeonholes in memory, so it forgets them when the
scheduler stops. That is fine: on the next start, every delivery still
marked `pending` is posted again, oldest first. A notifier that already
said "got it" (`sent`) is never sent the alert a second time.

The alert is also *published* on its topic, so anyone listening live (a
dashboard, say) hears about it at once.

## Knock Again, Then Give Up

A notifier's `retries:` decide how patient its postman is:

```yaml
notifiers:
  ops-slack:
    type: slack
    webhook_url: "${SLACK_URL}"
    timeout: 10s
    retries: {count: 5, delay: 10s, max_delay: 10m}
```

After a failed try the copy waits `delay`, then twice that, then four
times, never more than `max_delay`. After `count` retries (so `count + 1`
tries in all) the delivery is `dead`. One send may take only the
notifier's `timeout` (plus a second's grace); a send that takes longer
counts as a failed try, "send timed out after 10s".

Each notifier has its own pigeonhole, and at most four of its letters are
out being sent at once. The slow part (the network, the disk) runs on a
team of eight helpers that all notifiers share, apart from the scheduler's
own helpers. A send that times out frees its place in the pigeonhole, but
the helper running it may stay stuck until the network gives up (a server
that never answers, a lookup that never ends), and the retry takes another
helper. So a notifier that stays broken for a long time can tie up every
helper and make the other notifiers wait their turn. It can never hold up
the scheduler itself: runs, the store and the API keep their own helpers.

Here is a tiny PyMQ queue whose postman always fails. After two tries the
letter lands in the dead letter box:

```python
import pynet.loop
from pymq.broker import Broker, LeasedMessage, QueueConfig


async def main() -> None:
    """Fail every delivery and watch the letter go dead."""
    broker = Broker()
    config = QueueConfig(max_attempts=2, retry_delay=0.01, max_retry_delay=0.01)
    broker.declare_queue("notify.ops-slack", config)

    def postman(message: LeasedMessage) -> None:
        """Nobody is home: put the letter back."""
        print("try", message.envelope.attempt)
        broker.nack(message.queue, message.delivery_tag, error="nobody home")

    broker.consume("notify.ops-slack", postman, prefetch=4)
    broker.put("notify.ops-slack", {"message": "load failed"}, id="a1|ops-slack")
    await pynet.loop.sleep(0.1)
    dead = broker.dead_letters("notify.ops-slack")
    print(len(dead), dead[0].error)  # 1 nobody home
    broker.close()


pynet.loop.run(main())
```

## Secrets Stay Secret

A web address taken from `${SLACK_URL}` often turns up in the error text
when a send fails ("cannot reach https://hooks.slack.com/..."). Before the
error is stored or logged, every value that came from a `${NAME}` is put
back as `${NAME}`, so the store says "cannot reach ${SLACK_URL}" and the
secret is never written down.

## Replaying the Dead Letter Box

A dead delivery stays in the store as `dead`, with its last error. Once
the problem is fixed (Slack is back), it can be **replayed**: the delivery
goes back to `pending` with zero tries and is posted again. Only dead
deliveries can be replayed; asking to replay one that was sent is an
error, so nobody is told twice by accident.

If a reload removes a notifier while some of its deliveries are still
waiting, those deliveries are marked `dead` with the error
`notifier removed`, and they cannot be replayed while that notifier is
gone (a replay of one answers 409, and the delivery stays `dead`). A
replay asked for after the scheduler has started stopping answers 503
`alerts are not running`. If a reload changes a notifier's retry
settings, the dispatcher opens a fresh pigeonhole for it
(`notify.<name>.1`, then `.2` and so on), because a PyMQ queue keeps the
settings it was made with. A copy still in an old pigeonhole follows the
**new** `count` all the same: it is given up on after the new number of
tries, and moved to the fresh pigeonhole if the old one would give up
too soon.
