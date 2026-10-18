"""The notifiers' own team of helper threads.

A webhook whose server never answers, an SMTP server that hangs, a slow
disk: each of those holds a thread until the network gives up. If they
borrowed the loop's shared helper threads, four stuck sends would leave
none for the store writer's commits and the whole scheduler would wait.
So every blocking notifier call runs here instead, on a team of its own,
like the launcher's (``engine/launcher.py``).

The team is never joined when the loop closes: a send stuck in a socket
must not stop the scheduler shutting down (the threads are daemons).
"""

import contextlib
import weakref
from collections.abc import Callable
from typing import cast

from pynet.loop import EventLoop, Future, WorkerPool, get_running_loop

__all__ = ["NOTIFIER_WORKERS", "notifier_busy", "run_notifier_io"]

NOTIFIER_WORKERS = 8
"""How many threads all notifiers share for their blocking work.

The dispatcher sends at most ``PREFETCH`` deliveries of one notifier at
a time, so one unreachable endpoint ties up at most that many, and the
other notifiers still have threads to send with.
"""

_NOTIFIER_POOL = WorkerPool(NOTIFIER_WORKERS)
_HOOKED_LOOPS: weakref.WeakSet[EventLoop] = weakref.WeakSet()


def notifier_busy() -> bool:
    """Return whether any notifier thread is still working on something."""
    return _NOTIFIER_POOL.busy()


def _pool(loop: EventLoop) -> WorkerPool:
    """Return the notifier threads, telling ``loop`` they count as real work in flight."""
    if loop not in _HOOKED_LOOPS:
        _HOOKED_LOOPS.add(loop)
        loop.add_busy_check(_NOTIFIER_POOL.busy)
    return _NOTIFIER_POOL


async def run_notifier_io[T](fn: Callable[[], T]) -> T:
    """Run ``fn`` on a notifier thread and await its result (or its exception).

    Use this, not ``pynet.loop.run_in_thread``, for any blocking work a
    notifier does. If the awaiting task is cancelled (a timeout), the
    thread still finishes on its own and its result is thrown away.

    Raises:
        RuntimeError: If no loop is running in this thread.

    """
    loop = get_running_loop()
    future: Future[T] = Future(loop)

    def deliver(value: T | None, error: BaseException | None) -> None:
        """On the notifier thread: hand the outcome to the loop, if it is still open."""

        def fill() -> None:
            """On the loop: fill the future unless it was cancelled meanwhile."""
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(cast("T", value))

        with contextlib.suppress(RuntimeError):  # the loop has closed: nobody to tell
            loop.call_soon_threadsafe(fill)

    _pool(loop).submit(fn, deliver)
    return await future
