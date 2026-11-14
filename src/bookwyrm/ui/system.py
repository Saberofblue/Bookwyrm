"""Small things that differ between macOS, Windows and Linux desktops."""

from __future__ import annotations

import os
import subprocess
import sys

MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"

FILE_MANAGER = "Finder" if MAC else "File Explorer" if WINDOWS else "the file manager"


def reveal(path) -> bool:
    """Show a file or folder in the system's file manager, selected if it is a file."""
    path = os.path.normpath(str(path))
    try:
        if MAC:
            subprocess.Popen(["open", "-R", path])
        elif WINDOWS:
            if os.path.isdir(path):
                subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
        return True
    except OSError:
        return False


CLOUD_MARKERS = ("dropbox", "onedrive", "icloud", "mobile documents", "google drive",
                 "googledrive", "cloudstorage", "box sync", "pcloud")


def looks_cloud_synced(path) -> bool:
    """Is this folder inside one that a cloud service keeps in sync?

    A catalogue database that is written while such a service copies it can be corrupted.
    """
    lowered = os.path.normpath(str(path)).lower().replace("\\", "/")
    return any(f"/{marker}" in lowered or lowered.startswith(marker) for marker in CLOUD_MARKERS)
