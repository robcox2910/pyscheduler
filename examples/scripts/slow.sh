#!/bin/sh
# A job that runs longer than its max_runtime, to raise a "too_long" alert.
set -eu
echo "slow: working for a while..."
sleep 20
echo "slow: done"
