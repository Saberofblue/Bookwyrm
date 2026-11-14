"""Workarounds for PyMuPDF quirks, verified against the pinned version by tests.

PyMuPDF's ``JM_have_operation`` asks MuPDF for the current undo step to decide
whether a journal operation is open, but MuPDF raises when called *during* an
operation. With journalling enabled that makes ``insert_text``, ``set_toc`` and
friends fail exactly when they are used correctly. Bookwyrm always wraps edits
in an operation itself, so the guard is replaced with a constant.

``new_page`` and ``insert_pdf`` have the same guard compiled into the C++
"extra" fast path; ``python_paths()`` routes those two calls through the
pure-Python implementations instead.
"""

from __future__ import annotations

from contextlib import contextmanager

import pymupdf

_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    pymupdf.JM_have_operation = lambda pdf: 1
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    _applied = True


@contextmanager
def python_paths():
    """Use PyMuPDF's pure-Python code paths for the duration of the block."""
    previous = pymupdf.g_use_extra
    pymupdf.g_use_extra = False
    try:
        yield
    finally:
        pymupdf.g_use_extra = previous
