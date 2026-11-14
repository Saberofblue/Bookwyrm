"""Shortcut hints written the way each platform writes them."""

from __future__ import annotations

import sys

from PySide6.QtGui import QKeySequence

MAC = sys.platform == "darwin"

# How to pick several things at once, for instructions shown to the user.
MULTI_SELECT = "⌘-click or ⇧-click" if MAC else "Ctrl-click or Shift-click"
# The key that makes a drag copy instead of move.
COPY_KEY = "⌥" if MAC else "Ctrl"


def hint(shortcut) -> str:
    """'⇧⌘F' on a Mac, 'Ctrl+Shift+F' on Windows. Accepts 'Ctrl+Shift+F' or a StandardKey."""
    return QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)
