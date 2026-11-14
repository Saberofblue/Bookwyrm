"""A plain-text error log, for when there is no console to print to.

On Windows the app and its helper processes run without a console, so an
unexpected error would otherwise vanish. Everything lands in ``bookwyrm.log``
inside the library folder (the path travels to helpers in ``BOOKWYRM_LOG``).
Qt-free: helpers use it too.
"""

from __future__ import annotations

import os
import sys
import time
import traceback

LIMIT = 512 * 1024  # start afresh once the log reaches this size


def path() -> str | None:
    return os.environ.get("BOOKWYRM_LOG") or None


def record(what: str, detail: str = "") -> None:
    target = path()
    if not target:
        return
    try:
        if os.path.exists(target) and os.path.getsize(target) > LIMIT:
            os.replace(target, target + ".old")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {what}\n")
            if detail:
                handle.write(detail.rstrip() + "\n")
    except OSError:
        pass  # logging must never be the thing that breaks


def record_exception(what: str) -> None:
    record(what, traceback.format_exc())


def install(target: str, role: str = "app") -> None:
    """Send every uncaught error in this process to the log at `target`."""
    os.environ["BOOKWYRM_LOG"] = str(target)
    previous = sys.excepthook

    def hook(kind, value, trace):
        record(f"unexpected error in the {role}",
               "".join(traceback.format_exception(kind, value, trace)))
        previous(kind, value, trace)

    sys.excepthook = hook
