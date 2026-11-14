"""What the Card Catalogue knows about pages: which tomes have no text to search, the
numbers printed on pages, and pinning a result to a Study Table."""

import sqlite3
from importlib import resources

import pymupdf
import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QPainter

from bookwyrm.db import database
from bookwyrm.db.repo import Library
from bookwyrm.library import worker
from bookwyrm.library.importer import Importer
from bookwyrm.ui.extras.boards import PagePinner, StudyTablePage, section_title
from bookwyrm.ui.extras.card_catalogue import INFO, ROLE, CardCatalogue
from bookwyrm.ui.extras.search_scope import scope_of
from bookwyrm.ui.library.bookcase_view import NO_TEXT_NOTE, Group, Section
from bookwyrm.ui.library.dialogs import searchable_text
from bookwyrm.ui.library.library_page import LibraryPage
from bookwyrm.ui.library.tome_list import TomeList
from bookwyrm.ui.theme import painters
from conftest import make_pdf

PROSE = "The silver dragon guards its hoard beneath the mountain."


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


def add_tome(library, title, pages, pdf=False, toc=True):
    """A ready tome whose page texts are `pages`; `pdf` gives it a real file of that length."""
    number = len(library.all_books()) + 1
    book = library.add_book(title=title, author="", filename=f"t{number}.pdf",
                            sha256=f"hash{number}", page_count=len(pages), file_size=1)
    library.mark_ready(book.id)
    if pdf:
        library.book_dir(book.id).mkdir(parents=True)
        make_pdf(library.book_file(book), pages=len(pages) if toc else min(3, len(pages)))
    library.add_page_text(book.id, list(enumerate(pages)))
    library.set_indexed(book.id)
    return library.book(book.id)


def labelled_pdf(path):
    """Six pages numbered i, ii and then 1 to 4, as a book with front matter is."""
    make_pdf(path, pages=6)
    doc = pymupdf.open(path)
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                         {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1}])
    doc.saveIncr()
    doc.close()
    return path


# -- which tomes have text to search ---------------------------------------------------------


def test_pages_with_text_are_counted_as_a_tome_is_read(library):
    assert database.has_text(PROSE) and not database.has_text("  12 \n\n ")
    assert not database.has_text("x" * 19) and database.has_text("x " * 20)
    book = library.add_book(title="Mixed", author="", filename="m.pdf", sha256="m",
                            page_count=4, file_size=1)
    library.mark_ready(book.id)
    assert library.book(book.id).text_pages is None and not library.book(book.id).unsearchable
    library.add_page_text(book.id, [(0, PROSE), (1, "  7  ")])  # text arrives in batches
    library.add_page_text(book.id, [(2, ""), (3, PROSE)])
    assert library.book(book.id).text_pages == 2
    assert not library.book(book.id).unsearchable  # not until it has been read to the end
    library.set_indexed(book.id)
    assert library.book(book.id).text_pages == 2 and not library.book(book.id).unsearchable

    scan = add_tome(library, "Scanned", ["", "  3 ", ""])
    assert scan.text_pages == 0 and scan.unsearchable
    empty = library.add_book(title="No pages", author="", filename="e.pdf", sha256="e",
                             page_count=0, file_size=1)
    library.mark_ready(empty.id)
    library.set_indexed(empty.id)  # nothing was ever added: none, not "unknown"
    assert library.book(empty.id).text_pages == 0
    library.clear_text(scan.id)  # about to be read again
    assert library.book(scan.id).text_pages is None and not library.book(scan.id).unsearchable
    library.add_page_text(scan.id, [(0, PROSE)])
    library.set_indexed(scan.id)
    assert library.book(scan.id).text_pages == 1


def version_two_catalogue(root):
    """A catalogue as Bookwyrm 0.2 left it, with two tomes read and one not yet."""
    (root / "books").mkdir(parents=True)
    schema = resources.files("bookwyrm.db").joinpath("schema.sql").read_text("utf-8")
    version_two = "\n".join(line for line in schema.splitlines()
                            if "text_pages" not in line and "page_labels" not in line)
    con = sqlite3.connect(root / "bookwyrm.db")
    con.executescript(version_two)
    for number, (title, indexed) in enumerate(
            [("Texty", 1), ("Scanned", 1), ("Unread", 0)], start=1):
        con.execute(
            """INSERT INTO books (id, title, filename, sha256, page_count, added_at, ready,
                                  indexed) VALUES (?, ?, 'x.pdf', ?, 3, '2026-11-13', 1, ?)""",
            (number, title, f"h{number}", indexed))
    con.executemany("INSERT INTO pages_fts (text, book_id, page) VALUES (?, ?, ?)", [
        (PROSE, 1, 0), ("   \n 9 \t", 1, 1), (PROSE + "\n" + PROSE, 1, 2),
        ("", 2, 0), ("12", 2, 1), ("", 2, 2), (PROSE, 3, 0)])
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()


def test_a_version_two_catalogue_is_upgraded_and_its_text_pages_counted(tmp_path):
    root = tmp_path / "old"
    version_two_catalogue(root)
    library = Library(root)  # opening it upgrades it
    assert library.db.execute("PRAGMA user_version").fetchone()[0] == 3
    books = {book.title: book for book in library.all_books()}
    assert books["Texty"].text_pages == 2 and not books["Texty"].unsearchable
    assert books["Scanned"].text_pages == 0 and books["Scanned"].unsearchable
    assert books["Unread"].text_pages is None  # counted when its reading is finished
    assert [t.book_id for t in library.search_tomes("dragon")] == [1, 3]  # the text survived
    assert {book.title for book in library.books_without_page_labels()} == set(books)
    library.set_page_labels(1, ["i", "1", "2"])
    assert library.printed_page(1, 2) == "2"
    library.close()

    # A copy of the catalogue as it was is kept, for going back to an older Bookwyrm.
    kept = sqlite3.connect(root / database.BACKUP_NAME)
    assert kept.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [row[0] for row in kept.execute("SELECT title FROM books ORDER BY id")] == [
        "Texty", "Scanned", "Unread"]
    assert "text_pages" not in [row[1] for row in kept.execute("PRAGMA table_info(books)")]
    kept.close()
    fresh = Library(tmp_path / "fresh")  # a new library has nothing to keep a copy of
    fresh.close()
    assert not (tmp_path / "fresh" / database.BACKUP_NAME).exists()
    assert not list(root.glob("*.partial"))


def test_a_failed_upgrade_leaves_the_catalogue_as_it_was(tmp_path, monkeypatch):
    root = tmp_path / "old"
    version_two_catalogue(root)
    monkeypatch.setitem(database.UPGRADES, 2,
                        "ALTER TABLE books ADD COLUMN text_pages INTEGER; "
                        "UPDATE no_such_table SET x = 1")
    with pytest.raises(sqlite3.OperationalError):
        Library(root)
    con = sqlite3.connect(root / "bookwyrm.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "text_pages" not in [row[1] for row in con.execute("PRAGMA table_info(books)")]
    con.close()
    monkeypatch.undo()
    library = Library(root)  # and the real upgrade still works afterwards
    assert library.book(2).unsearchable
    library.close()


def test_the_catalogue_points_out_tomes_that_cannot_be_searched(qtbot, library):
    case = library.add_place("bookcase", "Games")
    texty = add_tome(library, "Rules", [PROSE, PROSE])
    scan = add_tome(library, "Beginner <Game>", ["", ""])
    library.place_book(scan.id, case.id)
    catalogue = CardCatalogue(library)
    qtbot.addWidget(catalogue)
    assert not catalogue.note.isHidden()
    assert catalogue.note.text() == ("“Beginner &lt;Game&gt;” cannot be searched: its pages "
                                     "are pictures, with no text in them.")
    catalogue.field.setText("dragon")
    catalogue.search()  # still said while results are listed
    assert not catalogue.note.isHidden() and catalogue.results.topLevelItemCount() == 1
    catalogue.set_scope(scope_of(books=[texty.id]))  # out of the search's reach: not mentioned
    assert catalogue.note.isHidden()
    catalogue.set_scope(scope_of(places=[case.id]))
    assert not catalogue.note.isHidden()
    assert catalogue.summary.text() == "No page in the Games bookcase mentions “dragon”."

    catalogue.set_scope(scope_of())
    add_tome(library, "Atlas", [""])
    add_tome(library, "Maps", ["", ""])
    catalogue.search()
    assert catalogue.note.text().startswith("“Atlas”, “Beginner &lt;Game&gt;” and “Maps” cannot")
    add_tome(library, "Zine", [""])
    catalogue.search()
    assert catalogue.note.text().startswith("4 tomes cannot be searched: their pages")
    assert "Which ones?" in catalogue.note.text()
    menu = catalogue.cannot_be_searched_menu()
    assert [action.text() for action in menu.actions()] == [
        "Atlas", "Beginner <Game>", "Maps", "Zine"]
    with qtbot.waitSignal(catalogue.openRequested) as opened:
        menu.actions()[1].trigger()
    assert opened.args == [scan.id, 0, ""]


def test_shelves_list_and_details_mark_a_tome_with_no_text(qtbot, library):
    scan = add_tome(library, "Scanned", ["", ""])
    part = add_tome(library, "Partly", [PROSE, "", PROSE])
    full = add_tome(library, "Wholly", [PROSE, PROSE])
    unread = library.add_book(title="Unread", author="", filename="u.pdf", sha256="u",
                              page_count=2, file_size=1)
    assert searchable_text(scan).startswith("Searchable text: none.")
    assert searchable_text(part) == "Searchable text: on 2 of 3 pages"
    assert searchable_text(full) == "Searchable text: on every page"
    assert searchable_text(library.book(unread.id)) == "Searchable text: still being read"
    listing = TomeList()
    qtbot.addWidget(listing)
    listing.set_sections([Section(None, "", [Group(None, None, [scan, full])])])
    tips = {listing.topLevelItem(i).text(0): listing.topLevelItem(i).toolTip(0)
            for i in range(listing.topLevelItemCount())}
    assert tips == {"Scanned": f"Scanned\n{NO_TEXT_NOTE}", "Wholly": "Wholly"}
    canvas = QImage(120, 160, QImage.Format.Format_RGB32)
    canvas.fill(0xFFFFFFFF)
    painter = QPainter(canvas)
    painters.draw_no_text_mark(painter, QRect(10, 10, 100, 140))
    painter.end()
    assert canvas.pixel(96, 136) != 0xFFFFFFFF  # the mark sits in the foot of the cover
    assert canvas.pixel(20, 20) == 0xFFFFFFFF  # and nowhere else


# -- the numbers printed on pages ------------------------------------------------------------


def test_printed_page_numbers_are_kept_per_tome(library):
    book = add_tome(library, "Guide", [PROSE] * 4)
    assert library.page_labels(book.id) is None and library.printed_page(book.id, 2) == "3"
    assert [b.id for b in library.books_without_page_labels()] == [book.id]
    library.set_page_labels(book.id, ["i", "ii", "1", ""])
    assert library.page_labels(book.id) == ["i", "ii", "1", ""]
    assert [library.printed_page(book.id, n) for n in range(5)] == ["i", "ii", "1", "4", "5"]
    assert library.books_without_page_labels() == []
    library.set_page_labels(book.id, None)  # examined again and found to record none
    assert library.page_labels(book.id) is None and library.printed_page(book.id, 0) == "1"
    assert library.books_without_page_labels() == []  # examined all the same
    library.set_page_labels(book.id, ["A"])
    library.delete_book(book.id)
    again = add_tome(library, "Guide again", [PROSE])
    assert library.page_labels(again.id) is None  # nothing left over from the removed tome
    heard = []
    library.subscribe(heard.append)
    library.set_page_labels(again.id, ["1"])
    assert heard == ["page_labels"]


def test_the_import_helper_reads_printed_page_numbers(tmp_path):
    labelled = labelled_pdf(tmp_path / "labelled.pdf")
    assert worker.printed_labels(pymupdf, str(labelled)) == ["i", "ii", "1", "2", "3", "4"]
    assert worker.printed_labels(pymupdf, str(make_pdf(tmp_path / "plain.pdf"))) is None


def test_imported_tomes_get_their_printed_numbers_and_older_ones_are_examined(
        qtbot, library, tmp_path):
    importer = Importer(library)
    try:
        with qtbot.waitSignal(importer.batchFinished, timeout=30000) as finished:
            importer.import_files([labelled_pdf(tmp_path / "labelled.pdf"),
                                   make_pdf(tmp_path / "plain.pdf", pages=3)])
        qtbot.waitUntil(lambda: not importer.busy(), timeout=30000)
        by_title = {library.book(b).page_count: b for b in finished.args[0]["added"]}
        labelled, plain = by_title[6], by_title[3]
        assert library.page_labels(labelled) == ["i", "ii", "1", "2", "3", "4"]
        assert library.printed_page(labelled, 3) == "2" and library.printed_page(plain, 2) == "3"
        assert library.books_without_page_labels() == []
        assert library.book(labelled).text_pages == 6

        # As in a library made before printed numbers were kept: nothing is recorded.
        with library.db:
            library.db.execute("DELETE FROM page_labels")
        library._printed.clear()
        assert len(library.books_without_page_labels()) == 2
        importer.resume()
        qtbot.waitUntil(lambda: not importer.busy(), timeout=30000)
        assert library.page_labels(labelled) == ["i", "ii", "1", "2", "3", "4"]
        assert library.page_labels(plain) is None and library.books_without_page_labels() == []
        assert library.book(labelled).indexed  # examined without being read all over again
    finally:
        importer.close()


def test_results_pins_and_notices_use_the_printed_number(qtbot, library):
    book = add_tome(library, "Guide", ["Front matter.", PROSE, PROSE])
    catalogue = CardCatalogue(library)
    qtbot.addWidget(catalogue)
    catalogue.field.setText("dragon")
    catalogue.search()
    assert catalogue.page_text(book.id, 1) == "p. 2"  # no printed numbers: position in the file
    library.set_page_labels(book.id, ["i", "1", "2"])
    assert catalogue.page_text(book.id, 1) == "p. 1" and catalogue.page_text(book.id, 0) == "p. i"
    table = library.add_table("Session")
    library.add_pin(table.id, book.id, 2, None, "Hoard")
    board = StudyTablePage(library)
    qtbot.addWidget(board)
    board.show_table(table.id)
    assert board.cards.item(0).text() == "Hoard\nGuide · p. 2"


# -- pinning a result to a Study Table --------------------------------------------------------


def test_section_title():
    toc = [[1, "First", 1], [1, " Second ", 3], [2, "Second-A", 4]]
    assert [section_title(toc, page) for page in range(5)] == [
        "First", "First", "Second", "Second-A", "Second-A"]
    assert section_title([], 3) == "" and section_title(None, 0) == ""
    assert section_title([[1, "Late", 5]], 1) == ""  # before the first entry


def test_a_page_is_pinned_with_its_picture_without_opening_the_tome(qtbot, library):
    book = add_tome(library, "Guide", [PROSE] * 6, pdf=True)  # has contents: First, Second…
    plain = add_tome(library, "Leaflet", [PROSE] * 3, pdf=True, toc=False)
    table = library.add_table("Session 12")
    pinner = PagePinner(library)
    try:
        with qtbot.waitSignal(pinner.pinned, timeout=30000) as done:
            assert pinner.pin(table.id, book.id, 2, "dragon")
        assert done.args == [table.id, book.id, 2]
        with qtbot.waitSignal(pinner.pinned, timeout=30000):
            assert pinner.pin(table.id, plain.id, 1, "dragon")
        first, second = library.pins(table.id)
        assert (first.book_id, first.page, first.label, first.rect) == (book.id, 2, "Second", None)
        assert first.thumb[:2] == b"\xff\xd8"  # a JPEG picture of the page
        picture = QImage.fromData(first.thumb)
        assert 0 < picture.width() <= 420 and picture.height() > picture.width()
        assert second.label == "dragon" and second.thumb  # no contents: named by the search
        qtbot.waitUntil(lambda: not pinner.busy(), timeout=5000)  # helpers are closed again

        assert not pinner.pin(9999, book.id, 0)  # no such table
        assert not pinner.pin(table.id, 9999, 0)  # no such tome
        assert len(library.pins(table.id)) == 2
    finally:
        pinner.close()


def test_a_tome_that_cannot_be_drawn_is_still_pinned(qtbot, library):
    broken = add_tome(library, "Broken", [PROSE])
    library.book_dir(broken.id).mkdir(parents=True)
    library.book_file(broken).write_bytes(b"this is not a PDF")
    table = library.add_table("Session")
    pinner = PagePinner(library)
    try:
        with qtbot.waitSignal(pinner.pinned, timeout=30000):
            assert pinner.pin(table.id, broken.id, 0, "dragon")
        pin = library.pins(table.id)[0]
        assert pin.thumb is None and pin.label == "dragon"
    finally:
        pinner.close()


def test_closing_or_forgetting_stops_a_pin_under_way(qtbot, library):
    book = add_tome(library, "Guide", [PROSE] * 6, pdf=True)
    table = library.add_table("Session")
    pinner = PagePinner(library)
    assert pinner.pin(table.id, book.id, 0)
    pinner.forget(book.id)  # as when the tome is being removed from the library
    assert not pinner.busy()
    assert pinner.pin(table.id, book.id, 0)
    pinner.close()  # as when the library is closed
    qtbot.wait(300)
    assert not pinner.busy() and library.pins(table.id) == []


def test_a_result_is_pinned_from_its_menu(qtbot, library, monkeypatch):
    book = add_tome(library, "Guide", ["Front matter.", "Nothing.", PROSE, "Nothing.", PROSE,
                                       "End."], pdf=True)
    library.set_page_labels(book.id, ["i", "ii", "1", "2", "3", "4"])
    table = library.add_table("Hunts & Heists")
    catalogue = CardCatalogue(library)
    qtbot.addWidget(catalogue)
    catalogue.field.setText("dragon")
    catalogue.search()
    tome_row = catalogue.results.topLevelItem(0)
    assert tome_row.data(0, INFO) == ("Guide", 2)
    pins = catalogue.menu_for(tome_row.child(1)).actions()[2].menu()  # the page at index 4
    assert [action.text() for action in pins.actions() if not action.isSeparator()] == [
        "Hunts && Heists", "New Study Table…"]
    notices = []
    catalogue.notice.connect(notices.append)
    with qtbot.waitSignal(catalogue.pinner.pinned, timeout=30000):
        pins.actions()[0].trigger()
    pin = library.pins(table.id)[0]
    assert (pin.book_id, pin.page, pin.label) == (book.id, 4, "Second-A") and pin.thumb
    assert notices == ["Pinned page 3 of “Guide” to “Hunts & Heists”."]

    monkeypatch.setattr(catalogue, "ask_table_name", lambda: "Session 13")
    pins = catalogue.menu_for(tome_row).actions()[2].menu()  # the tome's row: its best page
    with qtbot.waitSignal(catalogue.pinner.pinned, timeout=30000):
        pins.actions()[-1].trigger()
    new_table = next(t for t in library.tables() if t.name == "Session 13")
    assert library.pins(new_table.id)[0].page == tome_row.data(0, ROLE)[1]
    monkeypatch.setattr(catalogue, "ask_table_name", lambda: None)  # the dialog is cancelled
    catalogue.menu_for(tome_row).actions()[2].menu().actions()[-1].trigger()
    assert len(library.tables()) == 2
    catalogue.pinner.close()


def test_the_library_page_passes_on_pin_notices_and_new_page_numbers(qtbot, library):
    book = add_tome(library, "Guide", [PROSE] * 6, pdf=True)
    table = library.add_table("Session")
    importer = Importer(library)
    page = LibraryPage(library, importer)
    qtbot.addWidget(page)
    try:
        page.show_catalogue("dragon")
        with qtbot.waitSignal(page.message, timeout=30000) as said:
            page.catalogue.pin(book.id, 1, table.id)
        assert said.args == ["Pinned page 2 of “Guide” to “Session”."]
        page.show_key(("table", table.id))
        assert page.table_page.cards.item(0).text().endswith("Guide · p. 2")
        library.set_page_labels(book.id, ["i", "ii", "1", "2", "3", "4"])
        qtbot.waitUntil(  # the cards follow once the news has settled
            lambda: page.table_page.cards.item(0).text().endswith("Guide · p. ii"), timeout=3000)
        page.detach()  # the library is about to close: no helper may be left running
        assert not page.catalogue.pinner.busy()
    finally:
        importer.close()
