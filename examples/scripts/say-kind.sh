#!/bin/sh
# Command notifier: read one alert (JSON) on stdin and print its kind.
set -eu
kind=$(sed -n 's/.*"kind": *"\([a-z_]*\)".*/\1/p' | head -n 1)
echo "ALERT $kind"
