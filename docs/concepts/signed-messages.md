# Signed Messages (The Wax Seal and the Date Stamp)

## A Letter You Can Trust

Long ago, a king sealed his letters with hot wax pressed with his ring.
The messenger might be anyone, the road might be dangerous, but the seal
told the reader two things: this really came from the king, and nobody
changed it on the way. A clever king also dated every letter and numbered
it, so an old order could not be slipped in again next year.

Every message between the scheduler and an agent is sealed like that:

- **The wax seal.** An HMAC-SHA256 of the message, made with the secret
  only the scheduler and that one agent know, travels in the `sig`
  header. Change one letter of the message and the seal no longer fits.
- **The date stamp.** Each message says when it was sent. One more than
  five minutes away from the reader's clock (either direction) is
  **stale** and refused. Keep the clocks of your machines roughly right.
- **The serial number.** Each message has a random one-time **nonce**. A
  nonce seen before is a **replay** and is refused. Nonces are
  remembered for up to ten minutes, twice the date window, which is the
  longest an old copy could still look fresh. The memory is kept in RAM,
  so it is lost when the scheduler (or the agent) restarts: a copy less
  than five minutes old can be played again just after a restart. That
  is harmless, because every report is idempotent on its attempt (a
  repeated `started` or `finished` changes nothing), and a repeated
  order for an attempt the agent already has never runs it twice.
- **The right kind of letter.** The scheduler only accepts reports
  (`hello`, `started`, `heartbeat`, `finished`, `log_reply`); an agent
  only accepts orders (`work`, `kill`, acks, log requests). A valid
  report cannot be played back as an order.

A refused message is logged with a short reason (`bad signature`, `stale`,
`replayed nonce`, `unknown agent`, ...) and dropped. The reason never
includes the secret or the contents.

!!! warning "What the seal does not do"
    The seal proves who wrote a message and that it is unchanged. It does
    **not** hide it: the broker has **no authentication and no encryption**,
    so anyone who can reach it can *read* every order, job command, job
    environment (`env:` values, `${ENV}` ones included), log line and
    report, and can flood the queues. Run the broker on loopback, on a
    trusted network, or through a tunnel (SSH or a VPN), and do not put
    secrets in a remote job's command line or environment.

## Secrets

Each agent has its own secret (32 characters or more) in the scheduler's
`agents.yaml`, and the same secret in the file the agent is started with
(`--secret-file`). They are never shown by the API, the web UI, logs or
problems, and never saved in the config history. A secret rotates by
editing the file on both sides; the scheduler picks the new one up on
[reload](remote-agents.md).

## Try It

```python
from datetime import UTC, datetime, timedelta

from pyscheduler.agents.signing import Rejected, Verifier, seal
from pyscheduler.engine.clock import FakeClock

SECRET = "s" * 40
now = datetime(2026, 10, 20, 9, tzinfo=UTC)
clock = FakeClock(now)
verifier = Verifier(
    lambda name: SECRET if name == "gpu-01" else None,
    clock=clock,
    accept=frozenset({"heartbeat"}),
)

body, headers = seal("heartbeat", "gpu-01", {"running": []}, SECRET, now=now)
print(verifier.check(body, headers).kind)  # heartbeat

for label, make in (
    ("replay", lambda: (body, headers)),
    ("forged", lambda: seal("heartbeat", "gpu-01", {}, "x" * 40, now=now)),
    ("stale", lambda: seal("heartbeat", "gpu-01", {}, SECRET, now=now - timedelta(minutes=9))),
):
    try:
        verifier.check(*make())
    except Rejected as exc:
        print(label, "->", exc.reason)
# replay -> replayed nonce
# forged -> bad signature
# stale -> stale
```
