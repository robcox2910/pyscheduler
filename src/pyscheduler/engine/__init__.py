"""The engine: the part of PyScheduler that watches the clock and runs jobs.

It does all its timing through a ``Clock`` and starts every attempt
through a ``Launcher``, so tests can swap in a fake clock and a fake
launcher and drive a whole day of jobs in a blink.
"""
