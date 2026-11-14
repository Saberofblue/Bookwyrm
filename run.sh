#!/bin/sh
# Launch Bookwyrm from the project folder.
cd "$(dirname "$0")" && exec .venv/bin/python -m bookwyrm "$@"
