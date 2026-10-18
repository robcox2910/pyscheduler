"""The pump starts queued runs in batches, so one loop turn never starts hundreds at once."""

from datetime import timedelta

from engine_helpers import START, MakeHarness

from pyscheduler.engine.core import PUMP_BATCH
from pyscheduler.engine.model import Run, RunState

JOBS = 200
"""How many jobs fire at the same minute."""

AT_0930 = START + timedelta(hours=9, minutes=30)
"""When they all fire."""

LATER = AT_0930 + timedelta(minutes=5)
"""Long after every attempt has ended."""


class TestPumpBatches:
    """A burst of startable runs is started a batch per loop turn."""

    def test_burst_is_started_in_batches(self, harness: MakeHarness) -> None:
        """200 runs due at once all start, but no single pump starts more than a batch."""
        jobs = "".join(
            f"  j{n:03d}:\n    command: q\n    schedule: '30 9 * * *'\n" for n in range(JOBS)
        )
        h = harness(f"jobs:\n{jobs}", settings=f"max_running: {JOBS}")
        per_pump: list[int] = []
        engine = h.engine
        real_pump = engine._pump  # pyright: ignore[reportPrivateUsage]
        real_start = engine._start_attempt  # pyright: ignore[reportPrivateUsage]

        def start(run: Run) -> bool:
            """Count the starts of the pump that is running now."""
            per_pump[-1] += 1
            return real_start(run)

        def pump() -> None:
            """Open a new count, then pump."""
            per_pump.append(0)
            real_pump()

        engine._start_attempt = start  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        engine._pump = pump  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]

        async def body() -> None:
            """Let the minute come and every attempt finish."""
            await h.advance_to(LATER)

        h.run_engine(body)
        assert sum(per_pump) == JOBS
        assert max(per_pump) == PUMP_BATCH
        assert h.launcher.max_concurrent == JOBS
        assert all(h.run(f"j{n:03d}").state is RunState.SUCCEEDED for n in range(JOBS))
