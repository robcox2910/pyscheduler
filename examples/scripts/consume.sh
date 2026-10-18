#!/bin/sh
# Pretend to load the CSV files the watcher found ($@ is the file list).
set -eu
for f in "$@"; do
    echo "consume: loading $f"
done
