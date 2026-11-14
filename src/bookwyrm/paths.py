"""Where Bookwyrm keeps the library."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path.home() / "Bookwyrm Library"


def library_root(configured: str | None = None) -> Path:
    """The library folder: $BOOKWYRM_LIBRARY, then the saved setting, then the default.

    Kept out of cloud-synced folders by default: syncing a live database file
    while it is being written can corrupt it.
    """
    override = os.environ.get("BOOKWYRM_LIBRARY")
    if override:
        return Path(override).expanduser()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_ROOT
