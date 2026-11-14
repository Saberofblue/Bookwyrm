#!/bin/sh
# Build dist/Bookwyrm.app on a Mac. Usage: packaging/build.sh [--zip]
set -e
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -e .
exec .venv/bin/python packaging/build.py "$@"
