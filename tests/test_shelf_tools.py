"""Sorting, filtering, labels, cover size, the list view and exporting."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QSettings, Qt

from bookwyrm.db.repo import Book, Library
from bookwyrm.library.importer import Importer
from bookwyrm.ui import prefs, system
from bookwyrm.ui.library.arrange import arrange, count, sort_books
from bookwyrm.ui.library.bookcase_view import COVER_H, Group, Section
from bookwyrm.ui.library.library_page import LibraryPage
from conftest import make_pdf

BLANK = Book(id=0, title="", author="", filename="x.pdf", sha256="", page_count=1, file_size=1,
             added_at="2026-01-01T00:00:00+00:00", last_opened_at=None, last_page=0,
             last_offset=0.0, last_zoom=1.0, last_zoom_mode="fit_width", notes="", ready=True,
             indexed=True, edited=False)


def tome(n, title, author="", added="2026-01-01", opened=None):
    return replace(BLANK, id=n, title=title, author=author, added_at=added, last_opened_at=opened)


BOOKS = [
    tome(1, "Motes of the Divine", "Loot Tavern", "2026-03-01", "2026-11-10"),
    tome(2, "heliana's Guide", "", "2026-01-01"),
    tome(3, "Crafting Catalogue", "L'Arsene", "2026-02-01", "2026-11-12"),
]


def titles(books):
    return [b.title for b in books]


def test_sort_orders():
    assert titles(sort_books(BOOKS, "shelf")) == titles(BOOKS)
    assert titles(sort_books(BOOKS, "title")) == [
        "Crafting Catalogue", "heliana's Guide", "Motes of the Divine"]  # case does not matter
    assert titles(sort_books(BOOKS, "author")) == [
        "Crafting Catalogue", "Motes of the Divine", "heliana's Guide"]  # no author goes last
    assert titles(sort_books(BOOKS, "added")) == [
        "Motes of the Divine", "Crafting Catalogue", "heliana's Guide"]
    assert titles(sort_books(BOOKS, "read")) == [
        "Crafting Catalogue", "Motes of the Divine", "heliana's Guide"]  # never opened goes last


def test_arrange_filters_and_drops_what_is_left_empty():
    sections = [
        Section(10, "Heliana", [Group(10, None, BOOKS[:2]), Group(11, "Supplements", [BOOKS[2]])]),
        Section(20, "Empty shelf", [Group(20, None, [])]),
    ]
    assert count(arrange(sections)) == 3 and len(arrange(sections)) == 2  # no filter: all kept

    by_text = arrange(sections, text="  HELIANA ")
    assert [s.title for s in by_text] == ["Heliana"]
    assert [(g.title, titles(g.books)) for g in by_text[0].groups] == [(None, ["heliana's Guide"])]

    assert titles(arrange(sections, text="loot")[0].groups[0].books) == ["Motes of the Divine"]

    labels = {1: ["Hunts"], 3: ["hunts", "Crafting"]}
    by_label = arrange(sections, label="HUNTS", labels_by_book=labels)
    assert [(g.title, titles(g.books)) for g in by_label[0].groups] == [
        (None, ["Motes of the Divine"]), ("Supplements", ["Crafting Catalogue"])]
    assert arrange(sections, text="zzz") == []
    assert arrange(sections, label="Crafting", text="motes", labels_by_book=labels) == []


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


def add_book(library, n, title, place_id=None, author="", pdf=False):
    book = library.add_book(title=title, author=author, filename=f"t{n}.pdf", sha256=f"h{n}",
                            page_count=10 + n, file_size=1000)
    library.mark_ready(book.id)
    if pdf:
        library.book_dir(book.id).mkdir(parents=True)
        make_pdf(library.book_file(book), pages=2)
    if place_id is not None:
        library.place_book(book.id, place_id)
    return library.book(book.id)


@pytest.fixture
def page(qtbot, library):
    importer = Importer(library)
    page = LibraryPage(library, importer)
    qtbot.addWidget(page)
    page.resize(1200, 760)
    page.show()
    yield page
    importer.close()


@pytest.fixture
def shelf(qtbot, library, page):
    case = library.add_place("bookcase", "D&D")
    shelf = library.add_place("shelf", "Hunts", case.id)
    for n, (title, author) in enumerate(
            [("Motes of the Divine", "Loot Tavern"), ("Heliana's Guide", "Loot Tavern"),
             ("Crafting Catalogue", "L'Arsene")], start=1):
        add_book(library, n, title, shelf.id, author, pdf=True)
    qtbot.wait(30)
    page.show_key(("place", shelf.id))
    return shelf


def shown(page):
    return [slot.book.title for slot in page.view._slots]


def test_filter_box_and_sort_menu_drive_the_shelf(qtbot, library, page, shelf):
    assert shown(page) == ["Motes of the Divine", "Heliana's Guide", "Crafting Catalogue"]
    assert page.tools.isVisible()
    page.sort_box.setCurrentIndex(page.sort_box.findData("title"))
    page._sort_chosen(0)
    assert shown(page) == ["Crafting Catalogue", "Heliana's Guide", "Motes of the Divine"]
    assert prefs.get("shelves/sort") == "title"

    page.filter_box.setText("loot")  # matches the author
    assert shown(page) == ["Heliana's Guide", "Motes of the Divine"]
    assert "showing 2 of 3 tomes" in page.subheading.text()
    page.filter_box.setText("nothing like this")
    assert shown(page) == [] and page.view._empty_text == "No tome here matches the filter."
    page.show_key(("virtual", "stacks"))  # moving elsewhere clears the typed filter
    assert page.filter_box.text() == "" and len(shown(page)) == 3

    page.show_key(("virtual", "catalogue"))
    assert not page.tools.isVisible()  # the tools belong to shelves only


def test_labels_are_set_from_the_menu_and_filter_the_shelf(qtbot, library, page, shelf):
    books = {b.title: b for b in library.all_books()}
    page._label([books["Heliana's Guide"], books["Motes of the Divine"]], "Hunts", True)
    page._label([books["Heliana's Guide"]], "Season 3", True)
    qtbot.wait(30)
    assert [page.label_box.itemText(i) for i in range(page.label_box.count())] == [
        "All labels", "Hunts", "Season 3"]
    page.label_box.setCurrentIndex(page.label_box.findText("Hunts"))
    page._show()
    assert sorted(shown(page)) == ["Heliana's Guide", "Motes of the Divine"]
    page._label([books["Motes of the Divine"]], "hunts", False)  # untick, any capitalisation
    qtbot.wait(30)
    assert shown(page) == ["Heliana's Guide"]
    assert library.labels_of(books["Motes of the Divine"].id) == []


def test_cover_size_slider_resizes_and_is_remembered(qtbot, library, page, shelf):
    view = page.view
    assert view.cover_h == COVER_H
    width = view._slots[0].rect.width()
    page.size_slider.setValue(240)
    assert view.cover_h == 240 and view._slots[0].rect.height() > 220
    assert view._slots[0].rect.width() > width
    assert prefs.get("shelves/cover_height") == 240
    page.size_slider.setValue(9999)  # clamped by the slider
    assert view.cover_h == 260
    view.set_cover_height(10)
    assert view.cover_h == 110


def test_list_view_shows_the_same_tomes(qtbot, library, page, shelf):
    books = {b.title: b for b in library.all_books()}
    library.set_labels(books["Heliana's Guide"].id, ["Hunts"])
    qtbot.wait(30)
    page.list_button.setChecked(True)
    assert page.shelves.currentWidget() is page.list and page.browser() is page.list
    assert prefs.get("shelves/view") == "list" and not page.size_slider.isEnabled()
    rows = [page.list.topLevelItem(i) for i in range(page.list.topLevelItemCount())]
    assert [r.text(0) for r in rows] == ["Motes of the Divine", "Heliana's Guide",
                                         "Crafting Catalogue"]
    heliana = rows[1]
    assert (heliana.text(1), heliana.text(2), heliana.text(3), heliana.text(6)) == \
        ("Loot Tavern", "12", "Hunts", "Hunts")
    heliana.setSelected(True)
    assert [b.title for b in page.browser().selected_books()] == ["Heliana's Guide"]
    assert page.browser().selected() == [(shelf.id, books["Heliana's Guide"].id)]
    with qtbot.waitSignal(page.openBook) as opened:
        page.list._activated(heliana, 0)
    assert opened.args == [books["Heliana's Guide"].id]
    def listed():
        return [page.list.topLevelItem(i).text(0) for i in range(page.list.topLevelItemCount())]
    for key, expected in (("title", ["Crafting Catalogue", "Heliana's Guide",
                                     "Motes of the Divine"]),
                          ("shelf", ["Motes of the Divine", "Heliana's Guide",
                                     "Crafting Catalogue"])):
        page.sort_box.setCurrentIndex(page.sort_box.findData(key))
        page._sort_chosen(0)
        assert listed() == expected  # the list follows the Sort menu
    page.list.sortByColumn(2, Qt.SortOrder.DescendingOrder)  # ...until a heading is clicked
    assert listed() == ["Crafting Catalogue", "Heliana's Guide", "Motes of the Divine"]
    page._show()
    assert listed() == ["Crafting Catalogue", "Heliana's Guide", "Motes of the Divine"]
    page.filter_box.setText("crafting")
    assert page.list.topLevelItemCount() == 1
    page.list_button.setChecked(False)
    assert page.shelves.currentWidget() is page.view and shown(page) == ["Crafting Catalogue"]


def test_export_copies_tomes_out_of_the_library(qtbot, library, page, shelf, tmp_path,
                                                monkeypatch):
    books = {b.title: b for b in library.all_books()}
    heliana = books["Heliana's Guide"]
    target = tmp_path / "out" / "My copy.pdf"
    target.parent.mkdir()
    monkeypatch.setattr("bookwyrm.ui.library.library_page.QFileDialog.getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    page.unsaved = lambda book_id: book_id == heliana.id
    with qtbot.waitSignal(page.message) as said:
        page.export([heliana])
    assert target.read_bytes() == library.book_file(heliana).read_bytes()
    assert "Unsaved changes are not in the copy" in said.args[0]
    assert QSettings().value("export_dir") == str(target.parent)

    folder = tmp_path / "many"
    folder.mkdir()
    (folder / "Motes of the Divine.pdf").write_bytes(b"already here")
    monkeypatch.setattr("bookwyrm.ui.library.library_page.QFileDialog.getExistingDirectory",
                        lambda *a, **k: str(folder))
    page.unsaved = lambda book_id: False
    page.export([books["Motes of the Divine"], books["Crafting Catalogue"]])
    assert sorted(p.name for p in folder.iterdir()) == [
        "Crafting Catalogue.pdf", "Motes of the Divine (2).pdf", "Motes of the Divine.pdf"]
    assert (folder / "Motes of the Divine.pdf").read_bytes() == b"already here"  # not overwritten

    archive = library.archive_file(heliana.id)
    archive.write_bytes(b"%PDF original")
    original = tmp_path / "out" / "first.pdf"
    monkeypatch.setattr("bookwyrm.ui.library.library_page.QFileDialog.getSaveFileName",
                        lambda *a, **k: (str(original), ""))
    page.export([heliana], original=True)
    assert original.read_bytes() == b"%PDF original"


def test_cloud_folders_are_recognised():
    assert system.looks_cloud_synced("/Users/me/Dropbox/Books")
    assert system.looks_cloud_synced(r"C:\\Users\\me\\OneDrive\\Documents\\Bookwyrm Library")
    assert system.looks_cloud_synced("/Users/me/Library/Mobile Documents/com~apple~CloudDocs/x")
    assert not system.looks_cloud_synced("/Users/me/Bookwyrm Library")
    assert not system.looks_cloud_synced(r"D:\\Books\\Library")


def test_preferences_have_typed_defaults(qapp):
    assert prefs.get("startup/reopen") is True and prefs.get("shelves/cover_height") == 172
    prefs.put("startup/reopen", False)
    prefs.put("shelves/cover_height", "200")  # some systems hand numbers back as text
    assert prefs.get("startup/reopen") is False and prefs.get("shelves/cover_height") == 200
    with pytest.raises(KeyError):
        prefs.put("no/such", 1)


# ---- minimizing shelves ----------------------------------------------------------------------

def test_a_shelf_can_be_minimized_and_brought_back(qtbot, library, page):
    case = library.add_place("bookcase", "D&D")
    hunts = library.add_place("shelf", "Hunts", case.id)
    rules = library.add_place("shelf", "Rules", case.id)
    for n, (title, shelf) in enumerate([("Heliana", hunts), ("Motes", hunts), ("PHB", rules)], 1):
        add_book(library, n, title, shelf.id)
    qtbot.wait(30)
    page.show_key(("place", case.id))
    view = page.view
    assert sorted(shown(page)) == ["Heliana", "Motes", "PHB"]
    full_height = view._content_h
    assert all(section.collapsible for _rail, section, _count in view._rails)

    rail = next(rail for rail, section, _ in view._rails if section.title == "Hunts")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rail.center())
    qtbot.wait(30)
    assert library.place(hunts.id).collapsed and not library.place(rules.id).collapsed
    assert shown(page) == ["PHB"] and view._content_h < full_height
    counts = {section.title: count for _rail, section, count in view._rails}
    assert counts == {"Hunts": 2, "Rules": 1}  # the minimized shelf still says what it holds
    assert "3 tomes" in page.subheading.text()

    page.filter_box.setText("motes")  # a filter looks inside minimized shelves too
    assert shown(page) == ["Motes"]
    page.filter_box.setText("")

    rail = next(rail for rail, section, _ in view._rails if section.title == "Hunts")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rail.center())
    qtbot.wait(30)
    assert not library.place(hunts.id).collapsed and len(shown(page)) == 3

    library.set_collapsed([hunts.id, rules.id], True)  # "Minimize All Shelves"
    qtbot.wait(30)
    assert shown(page) == [] and len(view._rails) == 2
    page.show_key(("place", hunts.id))  # looking at one shelf on its own always shows it
    assert sorted(shown(page)) == ["Heliana", "Motes"]
    assert not view._rails[0][1].collapsible


def test_tomes_can_be_dropped_on_a_minimized_shelf(qtbot, library, page):
    case = library.add_place("bookcase", "D&D")
    hunts = library.add_place("shelf", "Hunts", case.id)
    library.set_collapsed([hunts.id], True)
    qtbot.wait(30)
    page.show_key(("place", case.id))
    rail = page.view._rails[0][0]
    assert page.view._zone_at(rail.center())[1] == hunts.id


def test_an_older_catalogue_is_upgraded_in_place(tmp_path):
    import sqlite3
    from importlib import resources

    from bookwyrm.db import database
    root = tmp_path / "old"
    (root / "books").mkdir(parents=True)
    schema = resources.files("bookwyrm.db").joinpath("schema.sql").read_text("utf-8")
    version_one = "\n".join(line for line in schema.splitlines() if "collapsed" not in line)
    con = sqlite3.connect(root / "bookwyrm.db")
    con.executescript(version_one)
    con.execute("INSERT INTO places (kind, name) VALUES ('bookcase', 'From before')")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    library = Library(root)  # opening it upgrades it
    assert library.db.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
    case = library.places()[0]
    assert case.name == "From before" and case.collapsed is False
    shelf = library.add_place("shelf", "New", case.id)
    library.set_collapsed([shelf.id], True)
    assert library.place(shelf.id).collapsed
    library.close()

    con = sqlite3.connect(root / "bookwyrm.db")
    con.execute("PRAGMA user_version = 99")  # as if a later Bookwyrm had made it
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer Bookwyrm"):
        Library(root)
