"""Moving a library to another folder, safely. Qt-free.

The library must be closed first (no open catalogue, no helper processes).
The rule throughout: the old library is not touched until every file has
arrived at the new place and been checked, so a failed or cancelled move
leaves things exactly as they were.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

# Everything Bookwyrm keeps in a library folder. Anything else found there is left alone.
OWNED = ("bookwyrm.db", "bookwyrm.db-wal", "bookwyrm.db-shm", "books", "covers", "excerpts",
         "bookwyrm.log", "bookwyrm.log.old", "bookwyrm-before-upgrade.db")
FOLDER_NAME = "Bookwyrm Library"


class MoveError(Exception):
    """Why a library could not be moved, phrased for the user."""


class Cancelled(Exception):
    """The user stopped the move; nothing was changed."""


def is_library(folder: Path) -> bool:
    return (Path(folder) / "bookwyrm.db").exists()


def resolve_target(chosen: Path) -> Path:
    """The library folder to use when the user picks `chosen`.

    A folder that already is a library, is empty, or does not exist yet is used
    as it is. A folder with other things in it gets a "Bookwyrm Library" folder
    inside, so the library's files never mix with someone's documents.
    """
    chosen = Path(chosen)
    if is_library(chosen) or not chosen.exists():
        return chosen
    if chosen.is_dir() and not any(chosen.iterdir()):
        return chosen
    return chosen / FOLDER_NAME


def owned_entries(root: Path) -> list[Path]:
    return [Path(root) / name for name in OWNED if (Path(root) / name).exists()]


def measure(root: Path) -> tuple[int, int]:
    """(number of files, total bytes) of a library's own files."""
    files = size = 0
    for entry in owned_entries(root):
        if entry.is_file():
            files, size = files + 1, size + entry.stat().st_size
            continue
        for folder, _dirs, names in os.walk(entry):
            for name in names:
                files += 1
                size += os.path.getsize(os.path.join(folder, name))
    return files, size


def check_move(old: Path, new: Path) -> None:
    """Raise MoveError if moving `old` to `new` cannot be right."""
    old, new = Path(old).resolve(), Path(new).resolve()
    if old == new:
        raise MoveError("That is where the library already is.")
    if old in new.parents:
        raise MoveError("The new folder is inside the current library folder. "
                        "Choose a folder somewhere else.")
    if is_library(new):
        raise MoveError("There is already a Bookwyrm library in that folder.")
    if new.exists() and not new.is_dir():
        raise MoveError("That is a file, not a folder.")
    if any((new / name).exists() for name in OWNED):
        raise MoveError("That folder already holds part of a Bookwyrm library. "
                        "Choose an empty folder.")


def move_library(old: Path, new: Path, progress=None, cancelled=None) -> None:
    """Move a closed library from `old` to `new`.

    `progress(done_bytes, total_bytes, name)` is called as files are copied;
    `cancelled()` returning True stops a copy and undoes it. On the same disk the
    move is a rename and is instant; across disks the files are copied, checked,
    and only then removed from the old place.
    """
    old, new = Path(old), Path(new)
    check_move(old, new)
    entries = owned_entries(old)
    try:
        new.mkdir(parents=True, exist_ok=True)
    except OSError as problem:
        raise MoveError(f"The folder could not be created: {problem}") from problem

    moved: list[tuple[Path, Path]] = []
    try:
        for entry in entries:  # same disk: each rename is instant
            target = new / entry.name
            os.rename(entry, target)
            moved.append((entry, target))
    except OSError as problem:
        for source, target in reversed(moved):  # put back whatever did move
            os.rename(target, source)
        if problem.errno != errno.EXDEV and moved:
            raise MoveError(f"The library could not be moved: {problem}") from problem
        _copy_then_remove(old, new, entries, progress, cancelled)
    _remove_if_empty(old)


def _copy_then_remove(old: Path, new: Path, entries: list[Path], progress, cancelled) -> None:
    files, total = measure(old)
    free = shutil.disk_usage(new).free
    if free < total * 1.02 + 16 * 1024 * 1024:
        raise MoveError(
            f"There is not enough room there: the library needs {_size(total)} and that "
            f"disk has {_size(free)} free.")
    done = 0
    copied: list[Path] = []
    try:
        for entry in entries:
            target = new / entry.name
            copied.append(target)
            if entry.is_file():
                shutil.copy2(entry, target)
                done += entry.stat().st_size
                if progress:
                    progress(done, total, entry.name)
                continue
            for folder, _dirs, names in os.walk(entry):
                destination = target / os.path.relpath(folder, entry)
                destination.mkdir(parents=True, exist_ok=True)
                for name in names:
                    if cancelled and cancelled():
                        raise Cancelled()
                    source = os.path.join(folder, name)
                    shutil.copy2(source, destination / name)
                    done += os.path.getsize(source)
                    if progress:
                        progress(done, total, name)
        if measure(new) != (files, total):
            raise MoveError("The copy did not match the original, so the library was "
                            "left where it was.")
    except BaseException as problem:
        for target in copied:  # leave no half-copied library behind
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        _remove_if_empty(new)
        if isinstance(problem, MoveError | Cancelled):
            raise
        if isinstance(problem, OSError):
            raise MoveError(f"The library could not be copied: {problem}") from problem
        raise
    for entry in entries:  # everything arrived and matched: now let the old copy go
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass


def _remove_if_empty(folder: Path) -> None:
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def _size(count: int) -> str:
    if count >= 1 << 30:
        return f"{count / (1 << 30):.1f} GB"
    return f"{max(1, count >> 20)} MB"


def describe_size(count: int) -> str:
    return _size(count)
