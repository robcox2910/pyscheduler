#!/bin/sh
# A job that always breaks, to raise a "failed" alert.
set -eu
echo "fail: something went wrong" >&2
exit 1
