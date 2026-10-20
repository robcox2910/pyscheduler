"""Run the PyScheduler two-agent demo.

    uv run python examples/agents_demo.py
    uv run python examples/agents_demo.py --folder /tmp/agents --seconds 30

It starts a scheduler and two agents (``build-01`` and ``gpu-01``) in one
process, runs a job pinned to one and a job tagged ``gpu``, and prints
which agent ran each. All the work is in ``pyscheduler.demo_agents``.
"""

import sys

from pyscheduler.demo_agents import main

if __name__ == "__main__":
    sys.exit(main())
