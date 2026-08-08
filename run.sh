#!/usr/bin/env bash
# Launch PediAid Voice Studio.
#
# Double-clickable from Finder, or run ./run.sh from a terminal.
# Run ./setup.sh first if the virtual environment does not exist yet.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo "The environment is not set up yet."
    echo "Run this first:  ./setup.sh"
    exit 1
fi

cd "$HERE"
exec "$VENV/bin/python" -m app "$@"
