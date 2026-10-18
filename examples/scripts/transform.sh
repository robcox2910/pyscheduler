#!/bin/sh
# Pretend to turn raw trades into warehouse rows.
set -eu
echo "transform: cleaning trades for $1"
sleep 1
echo "transform: 1180 rows (20 duplicates dropped)"
