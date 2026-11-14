"""The Settings window, moving or switching the library, and reopening last session's tomes."""

import sys

import pytest
from PySide6.QtCore import QSettings

from bookwyrm.app import Shell
from bookwyrm.db.repo import Library
from bookwyrm.ui import prefs
from bookwyrm.ui.main_window import MainWindow
from bookwyrm.ui.settings import SettingsDialog
from conftest import make_pdf


def add_tome(library, n=1):
    book = library.add_book(title=f"Tome {n}", author="", filename=f"t{n}.pdf",
                            sha256=f"hash{n}", page_count=6, file_size=1000)
    library.mark_ready(book.id)
    library.book_dir(book.id).mkdir(parents=True)
    make_pdf(library.book_file(book))
    return library.book(book.id)


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture
def dialog(qtbot, library, monkeypatch):
    monkeypatch.delenv("BOOKWYRM_LIBRARY", raising=False)
    dialog = SettingsDialog(library)
    qtbot.addWidget(dialog)
    dialog.answers = {"confirm": True, "choice": "move"}
    dialog.asked = []
    monkeypatch.setattr(dialog, "_confirm",
                        lambda title, message, action: dialog.asked.append(title)
                        or dialog.answers["confirm"])
    monkeypatch.setattr(dialog, "_ask_move_or_empty", lambda target: dialog.answers["choice"])
    return dialog


def choose(dialog, monkeypatch, folder):
    monkeypatch.setattr(dialog, "_pick_folder", lambda start: str(folder))
    requests = []
    dialog.libraryChangeRequested.connect(lambda target, mode: requests.append((target, mode)))
    dialog.change_location()
    return requests


def test_preferences_are_saved_as_they_are_changed(dialog, qtbot):
    assert dialog.reopen.isChecked() and dialog.location.text() == str(dialog.library.root)
    dialog.reopen.setChecked(False)
    assert prefs.get("startup/reopen") is False
    dialog.zoom.setCurrentIndex(dialog.zoom.findData("fit_page"))
    dialog.zoom.activated.emit(dialog.zoom.currentIndex())
    assert prefs.get("reader/zoom") == "fit_page"
    with qtbot.waitSignal(dialog.readingChanged):
        dialog.side_panel.setChecked(False)
    assert prefs.get("reader/side_panel") is False
    with qtbot.waitSignal(dialog.coverHeightChanged) as changed:
        dialog.cover.setValue(230)
    assert changed.args == [230]


def test_choosing_an_empty_folder_offers_to_move(dialog, monkeypatch, tmp_path):
    target = tmp_path / "new home"
    assert choose(dialog, monkeypatch, target) == [(str(target), "move")]
    dialog.answers["choice"] = "switch"  # "Start Empty"
    assert choose(dialog, monkeypatch, target)[-1] == (str(target), "switch")
    dialog.answers["choice"] = None  # cancelled
    assert choose(dialog, monkeypatch, tmp_path / "other") == []


def test_a_folder_with_other_things_gets_its_own_library_folder(dialog, monkeypatch, tmp_path):
    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "taxes.txt").write_text("x")
    assert choose(dialog, monkeypatch, documents) == [
        (str(documents / "Bookwyrm Library"), "move")]


def test_a_folder_holding_a_library_is_switched_to_not_overwritten(dialog, monkeypatch,
                                                                    tmp_path):
    Library(tmp_path / "other").close()
    assert choose(dialog, monkeypatch, tmp_path / "other") == [(str(tmp_path / "other"), "switch")]
    assert dialog.asked == ["Use That Library"]
    dialog.answers["confirm"] = False
    assert choose(dialog, monkeypatch, tmp_path / "other") == []


def test_synced_folders_get_a_warning_and_nonsense_is_refused(dialog, monkeypatch, tmp_path):
    synced = tmp_path / "Dropbox" / "Books"
    dialog.answers["confirm"] = False
    assert choose(dialog, monkeypatch, synced) == [] and dialog.asked == ["A Synced Folder"]
    dialog.answers["confirm"] = True
    assert choose(dialog, monkeypatch, synced) == [(str(synced), "move")]

    complaints = []
    monkeypatch.setattr("bookwyrm.ui.settings.dialogs.complain",
                        lambda parent, text: complaints.append(text))
    assert choose(dialog, monkeypatch, dialog.library.root) == []
    assert choose(dialog, monkeypatch, dialog.library.root / "books" / "inside") == []
    assert "already is" in complaints[0] and "inside the current library" in complaints[1]


def test_location_is_locked_when_set_from_outside(qtbot, library, monkeypatch):
    monkeypatch.setenv("BOOKWYRM_LIBRARY", str(library.root))
    dialog = SettingsDialog(library)
    qtbot.addWidget(dialog)
    assert not dialog.change.isEnabled() and "BOOKWYRM_LIBRARY" in dialog.location_note.text()


# ---- the shell: moving and switching for real --------------------------------------------

@pytest.fixture
def shell(qapp, tmp_path, monkeypatch):
    monkeypatch.delenv("BOOKWYRM_LIBRARY", raising=False)
    monkeypatch.setenv("BOOKWYRM_LOG", "")
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # Shell.open installs a log hook
    shell = Shell(qapp, (1000, 700))
    shell.open(tmp_path / "first")
    yield shell
    shell.window.close()
    shell.close()


def test_moving_the_library_takes_everything_and_reopens_it(shell, tmp_path, qtbot):
    first, target = tmp_path / "first", tmp_path / "second"
    book = add_tome(shell.library)
    case = shell.library.add_place("bookcase", "Kept")
    shell.library.place_book(book.id, case.id)
    pane = shell.window.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    old_window = shell.window

    shell.change_library(str(target), "move")
    assert shell.window is not old_window and shell.library.root == target
    assert QSettings().value("library_root") == str(target)
    assert not (first / "bookwyrm.db").exists()  # nothing left behind
    moved = shell.library.all_books()
    assert [b.title for b in moved] == ["Tome 1"]
    assert shell.library.book_file(moved[0]).exists()
    assert [p.name for p in shell.library.places()] == ["Kept"]
    qtbot.waitUntil(lambda: len(shell.window._panes()) == 1, timeout=15000)  # reopened there
    assert "now lives in" in shell.window._status.text()


def test_switching_uses_another_library_and_leaves_the_first_alone(shell, tmp_path):
    add_tome(shell.library)
    other = Library(tmp_path / "other")
    other.add_place("bookcase", "Elsewhere")
    other.close()
    shell.change_library(str(tmp_path / "other"), "switch")
    assert shell.library.root == tmp_path / "other"
    assert [p.name for p in shell.library.places()] == ["Elsewhere"]
    assert (tmp_path / "first" / "bookwyrm.db").exists()  # untouched
    shell.change_library(str(tmp_path / "brand new"), "switch")  # "Start Empty"
    assert shell.library.all_books() == [] and (tmp_path / "brand new" / "bookwyrm.db").exists()


def test_a_move_that_fails_reopens_the_library_where_it_was(shell, tmp_path, monkeypatch):
    add_tome(shell.library)
    warnings = []
    monkeypatch.setattr("bookwyrm.ui.settings.move_with_progress",
                        lambda old, new, parent=None: "There is not enough room there.")
    monkeypatch.setattr("bookwyrm.app.QMessageBox.warning",
                        lambda parent, title, text: warnings.append(text))
    shell.change_library(str(tmp_path / "second"), "move")
    assert shell.library.root == tmp_path / "first"
    assert len(shell.library.all_books()) == 1
    assert QSettings().value("library_root") is None
    assert "not enough room" in warnings[0]
    monkeypatch.setattr("bookwyrm.ui.settings.move_with_progress",
                        lambda old, new, parent=None: "cancelled")
    shell.change_library(str(tmp_path / "second"), "move")
    assert shell.library.root == tmp_path / "first" and len(warnings) == 1  # no nagging


def test_move_with_progress_really_moves(qapp, tmp_path):
    from bookwyrm.ui.settings import move_with_progress
    library = Library(tmp_path / "a")
    add_tome(library)
    library.close()
    assert move_with_progress(tmp_path / "a", tmp_path / "b") == ""
    assert (tmp_path / "b" / "bookwyrm.db").exists() and not (tmp_path / "a").exists()
    Library(tmp_path / "c").close()
    refused = move_with_progress(tmp_path / "b", tmp_path / "c")  # a message, never an exception
    assert "already a Bookwyrm library" in refused and (tmp_path / "b" / "bookwyrm.db").exists()


# ---- reopening where you left off -----------------------------------------------------------

def reopen(qtbot, library):
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)  # the restore runs on the first turn of the event loop
    return window


def test_last_sessions_tomes_come_back_on_their_sides(qtbot, library):
    one, two, three = add_tome(library, 1), add_tome(library, 2), add_tome(library, 3)
    window = reopen(qtbot, library)
    assert window._panes() == []  # nothing to restore the first time
    window.open_book(one.id)
    window.open_book(two.id)
    window.move_to_other_side()  # "Tome 2" to the right-hand group
    third = window.open_book(three.id)
    qtbot.waitUntil(third.view.is_idle, timeout=15000)
    window._focus_pane(window.left.widget(1))  # reading "Tome 1" when Bookwyrm closes
    window.close()

    again = reopen(qtbot, library)
    assert [p.book_id for p in again._panes()] == [one.id, three.id, two.id]
    assert again.right.count() == 1 and again.right.widget(0).book_id == two.id
    assert again.current_pane().book_id == one.id
    again.close()

    library.delete_book(three.id)  # a tome removed in the meantime is simply skipped
    prefs.put("startup/reopen", True)
    third_time = reopen(qtbot, library)
    assert [p.book_id for p in third_time._panes()] == [one.id, two.id]
    third_time.close()

    prefs.put("startup/reopen", False)
    assert reopen(qtbot, library)._panes() == []


def test_a_tome_opened_for_the_first_time_follows_the_zoom_preference(qtbot, library):
    book = add_tome(library)
    prefs.put("reader/zoom", "fit_page")
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.show()
    pane = window.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    assert pane.view.zoom_mode() == "fit_page"
    pane.view.set_zoom(1.5)
    window.close()
    again = MainWindow(library)
    qtbot.addWidget(again)
    again.show()
    pane = again.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    assert pane.view.zoom() == pytest.approx(1.5)  # after that, each tome remembers its own
    again.close()
