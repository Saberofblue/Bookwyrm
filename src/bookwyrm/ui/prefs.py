"""The user's preferences: a small typed layer over QSettings.

Every preference has a default here, so the rest of the app never repeats one.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

DEFAULTS = {
    "startup/reopen": True,  # reopen last session's tomes when Bookwyrm starts
    # for a tome opened for the first time: fit_width | fit_page | actual
    "reader/zoom": "fit_width",
    "reader/mode": "day",  # day | candle | night
    "reader/side_panel": True,
    "reader/spread": False,  # facing pages side by side
    "shelves/cover_height": 172,
    "shelves/view": "covers",  # covers | list
    "shelves/sort": "shelf",  # shelf | title | author | added | read
    "catalogue/order": "best",  # search results by tome: best | pages | title
}


def get(key: str):
    default = DEFAULTS[key]
    value = QSettings().value(key, default)
    if isinstance(default, bool):  # stored as text on some systems
        return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return str(value) if value is not None else default


def put(key: str, value) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    QSettings().setValue(key, value)
