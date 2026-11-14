import pymupdf
import pytest

from bookwyrm.db.repo import Library
from bookwyrm.library import worker
from bookwyrm.library.importer import Importer, safe_filename
from bookwyrm.ui.theme.theme import HERE as THEME_DIR
from conftest import make_pdf

FONTS = THEME_DIR / "fonts"


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture
def importer(library, qtbot):
    imp = Importer(library)
    yield imp
    imp.close()


def wait_idle(qtbot, importer):
    qtbot.waitUntil(lambda: not importer.busy(), timeout=30000)


@pytest.mark.parametrize("meta, filename, expected", [
    ("Heliana's Guide", "x.pdf", "Heliana's Guide"),
    ("", "Heliana_2nd_Edition_-_PDF.pdf", "Heliana 2nd Edition - PDF"),
    ("Untitled", "Ryoko_s_Guide (2).pdf", "Ryoko s Guide"),
    ("layout.indd", "Motes_of_the_Divine.pdf", "Motes of the Divine"),
    ("  ", "___.pdf", "Untitled tome"),
])
def test_clean_title(meta, filename, expected):
    assert worker.clean_title(meta, filename) == expected


def test_safe_filename():
    assert safe_filename("/a/b/My: Book?.pdf") == "My_ Book_.pdf"
    assert safe_filename("/a/b/scan") == "scan.pdf"


@pytest.mark.parametrize("given, expected", [
    ("CON.pdf", "_CON.pdf"),  # device names Windows refuses
    ("com1.PDF", "_com1.pdf"),
    ("nul.backup.pdf", "_nul.backup.pdf"),
    ("Trailing dots... .pdf", "Trailing dots.pdf"),
    ("   .pdf", "tome.pdf"),
    ("x" * 300 + ".pdf", "x" * 120 + ".pdf"),  # keeps paths under Windows' length limit
    ("Console.pdf", "Console.pdf"),
])
def test_safe_filename_is_legal_on_windows(given, expected):
    assert safe_filename(given) == expected


def test_page_texts_expand_ligatures_and_join_hyphens(tmp_path):
    path = tmp_path / "t.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    garamond = str(FONTS / "EBGaramond[wght].ttf")  # has a real "fi" ligature glyph
    page.insert_text((72, 72), "The \ufb01re drake was empow-", fontsize=12,
                     fontname="garamond", fontfile=garamond)
    page.insert_text((72, 90), "ered by frost.", fontsize=12,
                     fontname="garamond", fontfile=garamond)
    assert "\ufb01" in page.get_text()  # the PDF really contains the ligature
    doc.save(path)
    doc.close()
    batches = list(worker.page_texts(pymupdf, str(path)))
    text = batches[0][1][0][1]
    assert "fire" in text
    assert "empowered" in text


def test_import_copies_indexes_and_shelves(qtbot, library, importer, tmp_path):
    source = make_pdf(tmp_path / "My_Test_Tome.pdf", pages=45, title="")
    original = source.read_bytes()
    case = library.add_place("bookcase", "A")
    with qtbot.waitSignal(importer.batchFinished, timeout=30000) as finished:
        importer.import_files([source], case.id)
    summary = finished.args[0]
    assert len(summary["added"]) == 1 and not summary["failed"]
    wait_idle(qtbot, importer)

    book = library.book(summary["added"][0])
    assert book.title == "My Test Tome"
    assert book.page_count == 45 and book.ready and book.indexed
    assert library.book_file(book).read_bytes() == original
    assert library.cover_file(book.id).stat().st_size > 500
    assert [p.id for p in library.placements(book.id)] == [case.id]
    assert source.read_bytes() == original  # the original is untouched
    hits = library.search("keyword-37")
    assert [(h.book_id, h.page) for h in hits] == [(book.id, 36)]


def test_importing_the_same_file_twice_adds_one_tome(qtbot, library, importer, tmp_path):
    source = make_pdf(tmp_path / "a.pdf", pages=3)
    twin = tmp_path / "copy-of-a.pdf"
    twin.write_bytes(source.read_bytes())
    shelf_owner = library.add_place("bookcase", "A")
    with qtbot.waitSignal(importer.batchFinished, timeout=30000):
        importer.import_files([source])
    with qtbot.waitSignal(importer.batchFinished, timeout=30000) as finished:
        importer.import_files([twin], shelf_owner.id)
    summary = finished.args[0]
    assert not summary["added"] and len(summary["duplicates"]) == 1
    assert len(library.all_books()) == 1
    book = library.all_books()[0]
    assert [p.id for p in library.placements(book.id)] == [shelf_owner.id]  # shelved, not copied
    wait_idle(qtbot, importer)


def test_unreadable_files_are_reported_not_added(qtbot, library, importer, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    good = make_pdf(tmp_path / "good.pdf", pages=2)
    with qtbot.waitSignal(importer.batchFinished, timeout=30000) as finished:
        importer.import_files([bad, good])
    summary = finished.args[0]
    assert len(summary["added"]) == 1
    assert [path for path, _ in summary["failed"]] == [str(bad)]
    assert len(library.all_books()) == 1
    wait_idle(qtbot, importer)


def test_resume_indexes_tomes_left_unindexed(qtbot, library, importer, tmp_path):
    source = make_pdf(tmp_path / "a.pdf", pages=4)
    with qtbot.waitSignal(importer.batchFinished, timeout=30000) as finished:
        importer.import_files([source])
    wait_idle(qtbot, importer)
    book_id = finished.args[0]["added"][0]
    library.clear_text(book_id)
    assert library.search("keyword-2") == []
    importer.resume()
    wait_idle(qtbot, importer)
    assert library.book(book_id).indexed
    assert len(library.search("keyword-2")) == 1
