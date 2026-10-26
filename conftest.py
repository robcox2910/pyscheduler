"""Shared Hypothesis profiles for the series.

Two profiles are registered, and ``ci`` is loaded by default:

- ``ci`` runs 100 examples with no deadline, a fixed seed and no example
  database, so the same code gives the same result on every machine and a
  green build can be trusted.
- ``thorough`` runs 2000 examples with a random seed, to hunt for the rare
  counterexample before a release.

Run the long one with ``uv run pytest --hypothesis-profile=thorough``.

This file is owned by ``series sync``. Edit it in the series home repo
(robcox2910.github.io), not here, or the next sync will put it back.
"""

from hypothesis import settings

settings.register_profile("ci", max_examples=100, deadline=None, derandomize=True, database=None)
settings.register_profile("thorough", max_examples=2000, deadline=None, derandomize=False)
settings.load_profile("ci")
