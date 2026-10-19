"""Seed and serve the PyScheduler web UI demo.

    uv run python examples/ui_demo.py                  # then open the printed address
    uv run python examples/ui_demo.py --seed-only      # just write and run the seed
    uv run python examples/ui_demo.py --folder /tmp/demo --seconds 60

Log in as ``admin`` with password ``demo``. All the work is in
``pyscheduler.demo``.
"""

import sys

from pyscheduler.demo import main

if __name__ == "__main__":
    sys.exit(main())
