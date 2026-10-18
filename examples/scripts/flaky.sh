#!/bin/sh
# Pretend to download exchange rates from a server that fails the first time.
#
# The first attempt of each run leaves a marker file in the state folder
# (examples/state) and fails; the retry finds the marker and succeeds.
set -eu
state="$(dirname "$0")/../state"
marker="$state/fetch-rates.$PYSCHED_RUN_ID.tried"
mkdir -p "$state"
if [ ! -e "$marker" ]; then
    touch "$marker"
    echo "fetch-rates: server busy for $1, try again later" >&2
    exit 1
fi
rm -f "$marker"
echo "fetch-rates: got 42 rates for $1 (attempt $PYSCHED_ATTEMPT)"
