"""Moving a library to another folder must never lose or half-move it."""

import errno
import os

import pytest

from bookwyrm.db.repo import Library
from bookwyrm.library import relocate
from bookwyrm.library.relocate import Cancelled, MoveError


def make_library(root, tomes=3):
    library = Library(root)
    case = library.add_place("bookcase", "Kept")
    for n in range(tomes):
        book = library.add_book(title=f"Tome {n}", author="", filename=f"t{n}.pdf",
                                sha256=f"h{n}", page_count=1, file_size=1)
        library.mark_ready(book.id)
        library.place_book(book.id, case.id)
        library.book_dir(book.id).mkdir(parents=True)
        library.book_file(book).write_bytes(b"%PDF " + bytes([65 + n]) * (2000 + n))
        library.cover_file(book.id).write_bytes(b"cover" * 10)
    library.close()
    return root


def snapshot(root):
    found = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            found[os.path.relpath(path, root)] = open(path, "rb").read()
    return found


def cross_disk(monkeypatch):
    """Make renames fail the way they do between two disks, forcing the copy path."""
    def rename(source, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(relocate.os, "rename", rename)


def test_resolve_target(tmp_path):
    empty, busy = tmp_path / "empty", tmp_path / "busy"
    empty.mkdir()
    busy.mkdir()
    (busy / "holiday.jpg").write_bytes(b"x")
    existing = make_library(tmp_path / "existing")
    assert relocate.resolve_target(tmp_path / "new") == tmp_path / "new"  # will be created
    assert relocate.resolve_target(empty) == empty
    assert relocate.resolve_target(existing) == existing  # already a library: use it
    assert relocate.resolve_target(busy) == busy / "Bookwyrm Library"  # keep files apart


def test_move_on_the_same_disk_is_a_rename_and_keeps_everything(tmp_path):
    old = make_library(tmp_path / "old")
    before = snapshot(old)
    (old / "my notes.txt").write_text("not Bookwyrm's")  # someone else's file stays put
    new = tmp_path / "elsewhere" / "Library"
    relocate.move_library(old, new)
    assert snapshot(new) == before
    assert (old / "my notes.txt").exists() and not (old / "books").exists()
    reopened = Library(new)
    assert len(reopened.all_books()) == 3 and len(reopened.places()) == 1
    assert all(reopened.book_file(b).exists() for b in reopened.all_books())
    reopened.close()


def test_move_across_disks_copies_checks_then_removes(tmp_path, monkeypatch):
    old = make_library(tmp_path / "old")
    before = snapshot(old)
    cross_disk(monkeypatch)
    seen = []
    new = tmp_path / "other disk"
    relocate.move_library(old, new, progress=lambda done, total, name: seen.append((done, total)))
    assert snapshot(new) == before
    assert not old.exists()  # emptied and removed
    assert seen and seen[-1][0] == seen[-1][1] == relocate.measure(new)[1]
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


def test_a_cancelled_move_changes_nothing(tmp_path, monkeypatch):
    old = make_library(tmp_path / "old")
    before = snapshot(old)
    cross_disk(monkeypatch)
    calls = []
    new = tmp_path / "new"
    with pytest.raises(Cancelled):
        relocate.move_library(old, new, cancelled=lambda: calls.append(1) or len(calls) > 2)
    assert snapshot(old) == before
    assert not new.exists()  # no half-copied library left behind


def test_a_failed_copy_leaves_the_library_where_it_was(tmp_path, monkeypatch):
    old = make_library(tmp_path / "old")
    before = snapshot(old)
    cross_disk(monkeypatch)
    real_copy, count = relocate.shutil.copy2, []

    def failing(source, target):
        count.append(1)
        if len(count) == 4:
            raise OSError(errno.EIO, "the disk was unplugged")
        return real_copy(source, target)
    monkeypatch.setattr(relocate.shutil, "copy2", failing)
    new = tmp_path / "new"
    with pytest.raises(MoveError, match="could not be copied"):
        relocate.move_library(old, new)
    assert snapshot(old) == before and not new.exists()


def test_not_enough_room_is_refused_before_anything_is_copied(tmp_path, monkeypatch):
    old = make_library(tmp_path / "old")
    cross_disk(monkeypatch)
    monkeypatch.setattr(relocate.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 100})())
    with pytest.raises(MoveError, match="not enough room"):
        relocate.move_library(old, tmp_path / "new")
    assert (old / "bookwyrm.db").exists()


def test_impossible_moves_are_refused(tmp_path):
    old = make_library(tmp_path / "old")
    other = make_library(tmp_path / "other")
    with pytest.raises(MoveError, match="already is"):
        relocate.check_move(old, old)
    with pytest.raises(MoveError, match="inside the current library"):
        relocate.check_move(old, old / "books" / "deeper")
    with pytest.raises(MoveError, match="already a Bookwyrm library"):
        relocate.check_move(old, other)
    half = tmp_path / "half"
    (half / "books").mkdir(parents=True)
    with pytest.raises(MoveError, match="part of a Bookwyrm library"):
        relocate.check_move(old, half)
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(MoveError, match="a file, not a folder"):
        relocate.check_move(old, a_file)
    assert (old / "bookwyrm.db").exists() and (other / "bookwyrm.db").exists()


def test_closing_a_library_leaves_one_complete_database_file(tmp_path):
    root = make_library(tmp_path / "lib")
    assert (root / "bookwyrm.db").exists()
    leftovers = [name for name in os.listdir(root) if name.endswith(("-wal", "-shm"))]
    assert leftovers == [] or all(os.path.getsize(root / n) == 0 for n in leftovers)
