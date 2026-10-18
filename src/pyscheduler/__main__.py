"""Run the ``pysched`` command line as ``python -m pyscheduler``."""

import sys

from pyscheduler.cli import main

if __name__ == "__main__":
    sys.exit(main())
