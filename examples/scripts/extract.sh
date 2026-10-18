#!/bin/sh
# Pretend to copy one day's trades out of the trading system.
set -eu
echo "extract: reading trades for $1 (run $PYSCHED_RUN_ID)"
sleep 1
echo "extract: 1200 trades"
