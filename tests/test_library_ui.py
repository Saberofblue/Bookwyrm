import pytest
from PySide6.QtCore import QPoint, Qt

from bookwyrm.db.repo import Library
from bookwyrm.library.importer import Importer
from bookwyrm.ui.library.bookcase_view import ROW_H
from bookwyrm.ui.library.dialogs import ImportDialog, TomeDetailsDialog
from bookwyrm.ui.library.library_page import LibraryPage
from bookwyrm.ui.main_window import MainWindow
from bookwyrm.ui.theme import theme
from conftest import make_pdf


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


def add_book(library, n, place_id=None, pdf=None):
    book = library.add_book(title=f"Tome {n}", author="", filename=f"t{n}.pdf",
                            sha256=f"hash{n}", page_count=10, file_size=1000)
    library.mark_ready(book.id)
    if pdf is not None:
        library.book_dir(book.id).mkdir(parents=True)
        make_pdf(library.book_file(book))
    if place_id is not None:
        library.place_book(book.id, place_id)
    return library.book(book.id)


@pytest.fixture
def page(qtbot, library):
    importer = Importer(library)
    page = LibraryPage(library, importer)
    qtbot.addWidget(page)
    page.resize(1100, 700)
    page.show()
    yield page
    importer.close()


def settle(qtbot):
    qtbot.wait(30)  # library changes refresh the page on the next event-loop turn


def test_stylesheet_has_no_unresolved_tokens(qapp):
    theme.apply(qapp)
    sheet = theme.stylesheet()
    assert "@" not in sheet
    assert theme.DISPLAY_FONT == "Cinzel" and theme.BODY_FONT == "EB Garamond"


def test_bookcase_shows_each_shelf_as_a_section_with_sets(qtbot, library, page):
    case = library.add_place("bookcase", "D&D")
    heliana = library.add_place("shelf", "Heliana", case.id)
    official = library.add_place("shelf", "Official", case.id)
    dlc = library.add_place("set", "DLC", heliana.id)
    add_book(library, 1, heliana.id)
    add_book(library, 2, dlc.id)
    add_book(library, 3, official.id)
    settle(qtbot)
    page.show_key(("place", case.id))
    sections = page.view._sections
    assert [s.title for s in sections] == ["Heliana", "Official"]
    assert [(g.title, len(g.books)) for g in sections[0].groups] == [(None, 1), ("DLC", 1)]
    assert page.heading.text() == "D&D"
    assert "3 tomes" in page.subheading.text()
    assert page.new_child.text() == "New Shelf"
    assert len(page.view._bookends) == 2 and len(page.view._plaques) == 1

    page.show_key(("place", dlc.id))
    assert page.subheading.text().startswith("Set in D&D ▸ Heliana")
    assert not page.new_child.isVisible()  # nothing goes inside a set


def test_builtin_spots(qtbot, library, page):
    case = library.add_place("bookcase", "A")
    shelved = add_book(library, 1, case.id)
    loose = add_book(library, 2)
    library.mark_opened(shelved.id)
    settle(qtbot)
    page.show_key(("virtual", "cart"))
    assert [b.id for b in page.view.selected_books()] == []
    assert [s.book.id for s in page.view._slots] == [loose.id]
    page.show_key(("virtual", "desk"))
    assert [s.book.id for s in page.view._slots] == [shelved.id]
    page.show_key(("virtual", "stacks"))
    assert page.view.tome_count() == 2


def test_dropping_tomes_moves_or_copies_them(qtbot, library, page):
    case = library.add_place("bookcase", "A")
    one = library.add_place("shelf", "One", case.id)
    two = library.add_place("shelf", "Two", case.id)
    book = add_book(library, 1, one.id)
    page._books_dropped([(book.id, one.id)], two.id, False)
    assert [p.id for p in library.placements(book.id)] == [two.id]
    page._books_dropped([(book.id, two.id)], one.id, True)
    assert {p.id for p in library.placements(book.id)} == {one.id, two.id}
    page._take_off_shelf([(one.id, book.id)])
    assert [p.id for p in library.placements(book.id)] == [two.id]


def test_view_selection_and_keyboard(qtbot, library, page):
    case = library.add_place("bookcase", "A")
    books = [add_book(library, n, case.id) for n in range(1, 4)]
    settle(qtbot)
    page.show_key(("place", case.id))
    view = page.view
    first = view._slots[0].rect.center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    assert [b.id for b in view.selected_books()] == [books[0].id]
    qtbot.keyClick(view, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert [b.id for b in view.selected_books()] == [books[0].id, books[1].id]
    with qtbot.waitSignal(view.openRequested) as opened:
        qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                          pos=view._slots[2].rect.center())
    assert opened.args == [books[2].id]
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(first.x(), first.y() + 2 * ROW_H))
    assert view.selected_books() == []


def test_tree_lists_places_with_counts_and_renames(qtbot, library, page):
    case = library.add_place("bookcase", "A")
    shelf = library.add_place("shelf", "S", case.id)
    add_book(library, 1, shelf.id)
    settle(qtbot)
    tree = page.tree
    item = tree._find(("place", shelf.id))
    assert item.text(0) == "S" and item.text(1) == "1"
    assert tree._find(("virtual", "stacks")).text(1) == "1"
    item.setText(0, "Renamed")
    assert library.place(shelf.id).name == "Renamed"


def test_import_dialog_offers_places_and_creates_only_legal_ones(qtbot, library):
    case = library.add_place("bookcase", "A")
    shelf = library.add_place("shelf", "S", case.id)
    dialog = ImportDialog(library, ["/x/one.pdf", "/x/two.pdf"], shelf.id)
    qtbot.addWidget(dialog)
    assert dialog.place_id() == shelf.id
    assert dialog._new_set.isEnabled() and dialog._new_shelf.isEnabled()
    dialog.picker.setCurrentItem(dialog.picker.topLevelItem(0))  # Returns Cart
    assert dialog.place_id() is None
    assert not dialog._new_shelf.isEnabled() and not dialog._new_set.isEnabled()
    dialog.picker.rebuild(select=case.id)
    dialog._sync_buttons()
    assert dialog._new_shelf.isEnabled() and not dialog._new_set.isEnabled()


def test_details_dialog_saves(qtbot, library):
    book = add_book(library, 1)
    dialog = TomeDetailsDialog(library, book)
    qtbot.addWidget(dialog)
    dialog.title.setText("Heliana's Guide")
    dialog.author.setText("Loot Tavern")
    dialog.labels.setText("monsters, hunts")
    dialog.notes.setPlainText("Season 3 source")
    dialog._save()
    saved = library.book(book.id)
    assert (saved.title, saved.author, saved.notes) == \
        ("Heliana's Guide", "Loot Tavern", "Season 3 source")
    assert library.labels_of(book.id) == ["hunts", "monsters"]


def test_window_opens_tomes_in_tabs_and_remembers_the_page(qtbot, library, monkeypatch):
    book = add_book(library, 1, pdf=True)
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    pane = window.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    assert window.tabs.count() == 2 and window.tabs.currentWidget() is pane
    assert window.open_book(book.id) is pane  # opening again focuses the same tab
    assert window.tabs.count() == 2
    assert [b.id for b in library.recent_books()] == [book.id]

    pane.view.set_zoom(1.5)
    pane.view.go_to_page(3)
    qtbot.waitUntil(lambda: pane.view.current_page() == 3, timeout=5000)
    window.close_tab(1)
    saved = library.book(book.id)
    assert saved.last_page == 3 and saved.last_zoom == pytest.approx(1.5)
    assert saved.last_zoom_mode == "custom"

    again = window.open_book(book.id)
    qtbot.waitUntil(lambda: again.view.is_idle() and again.view.current_page() == 3,
                    timeout=15000)
    assert again.view.zoom() == pytest.approx(1.5)
    window.close()


def test_shortcut_hints_follow_the_platform(qapp):
    import sys

    from bookwyrm.ui import keys
    text = keys.hint("Ctrl+Shift+F")
    if sys.platform == "darwin":
        assert text == "⇧⌘F" and "⌘" in keys.MULTI_SELECT
    else:
        assert text == "Ctrl+Shift+F" and keys.MULTI_SELECT.startswith("Ctrl")


def test_removing_a_tome_closes_its_tabs_first(qtbot, library, monkeypatch):
    monkeypatch.setattr("bookwyrm.ui.library.library_page.dialogs.confirm", lambda *a: True)
    book = add_book(library, 1, pdf=True)
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.show()
    pane = window.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    window.library_page._remove_from_library([book])
    assert window.tabs.count() == 1  # the tome's tab went first, releasing its file
    assert library.book(book.id) is None and not library.book_dir(book.id).exists()
    window.close()
