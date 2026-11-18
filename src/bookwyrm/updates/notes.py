"""The two notes an update leaves in its staging folder, the same on every system.

    outcome.txt   written by the swap step: "installed", or "failed: <why>"
    started       written by a version of Bookwyrm when it has come up, so that a swap
                  step waiting to see whether the new version works can stop waiting

Both are read and written by different versions of Bookwyrm (the one that started an
update and the one it installed), so their names and meaning must not change.
"""

from __future__ import annotations

from pathlib import Path

OUTCOME, STARTED = "outcome.txt", "started"
INSTALLED = "installed"  # the outcome note left when the swap went through


def write_outcome(staging: Path, text: str) -> None:
    try:
        (Path(staging) / OUTCOME).write_text(text + "\n", "utf-8")
    except OSError:
        pass


def take_outcome(staging: Path) -> tuple[bool, str] | None:
    """What the last swap left to say, if anything: (it worked, the note). Reading it
    removes it, so it is reported once."""
    note = Path(staging) / OUTCOME
    try:
        text = note.read_text("utf-8").strip()
        note.unlink()
    except OSError:
        return None
    if not text:
        return None
    return (text == INSTALLED, text.removeprefix("failed:").strip())


def mark_started(staging: Path) -> None:
    """Tell a waiting swap step that this version has come up."""
    try:
        if Path(staging).is_dir():
            (Path(staging) / STARTED).write_text("", "utf-8")
    except OSError:
        pass
