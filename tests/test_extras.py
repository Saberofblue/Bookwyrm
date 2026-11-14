import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from bookwyrm.db.repo import Library, LibraryError
from bookwyrm.library.importer import Importer
from bookwyrm.ui.extras.boards import CommonplaceBook, StudyTablePage, image_bytes
from bookwyrm.ui.library.library_page import LibraryPage
from bookwyrm.ui.main_window import MainWindow
from conftest import make_pdf


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


def add_book(library, n=1, pdf=False):
    book = library.add_book(title=f"Tome {n}", author="", filename=f"t{n}.pdf",
                            sha256=f"hash{n}", page_count=6, file_size=1000)
    library.mark_ready(book.id)
    if pdf:
        library.book_dir(book.id).mkdir(parents=True)
        make_pdf(library.book_file(book))
    return library.book(book.id)


def png(color=0xFF336699, size=(40, 30)) -> bytes:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(color)
    return image_bytes(image)


@pytest.fixture
def window(qtbot, library, monkeypatch):
    for target in ("bookwyrm.ui.main_window.QSettings", "bookwyrm.ui.reader.reader_pane.QSettings"):
        monkeypatch.setattr(f"{target}.value", lambda self, key, default=None, **_kw: default)
        monkeypatch.setattr(f"{target}.setValue", lambda self, key, value: None)
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.resize(1300, 800)
    window.show()
    yield window
    window.close()


# ---- catalogue layer ---------------------------------------------------------------

def test_study_tables_and_pins(library):
    book = add_book(library)
    table = library.add_table("Session 12")
    with pytest.raises(LibraryError):
        library.add_table("  ")
    first = library.add_pin(table.id, book.id, 3, None, "Boss", b"thumb")
    second = library.add_pin(table.id, book.id, 5, (1, 2, 30, 40), "Map")
    pins = library.pins(table.id)
    assert [(p.page, p.label, p.rect) for p in pins] == [(3, "Boss", None),
                                                         (5, "Map", (1, 2, 30, 40))]
    assert pins[0].thumb == b"thumb"
    library.reorder_pins(table.id, [second, first])
    assert [p.id for p in library.pins(table.id)] == [second, first]
    library.relabel_pin(first, "Final boss")
    assert library.pins(table.id)[1].label == "Final boss"
    assert library.pin_counts() == {table.id: 2}
    library.delete_book(book.id)  # pins of a removed tome go with it
    assert library.pins(table.id) == []
    library.rename_table(table.id, "Session 13")
    assert library.table(table.id).name == "Session 13"
    library.delete_table(table.id)
    assert library.tables() == []


def test_excerpts_are_files_with_a_backlink(library):
    book = add_book(library)
    excerpt = library.add_excerpt(png(), book.id, 4, (10, 20, 110, 220), "Stat block")
    path = library.excerpt_file(excerpt)
    assert path.exists() and path.read_bytes().startswith(b"\x89PNG")
    assert (excerpt.book_id, excerpt.page, excerpt.rect, excerpt.note) == \
        (book.id, 4, (10, 20, 110, 220), "Stat block")
    library.annotate_excerpt(excerpt.id, "Use in session 12")
    assert library.excerpts()[0].note == "Use in session 12"
    library.delete_book(book.id)  # the excerpt survives, without its source
    assert library.excerpts()[0].book_id is None
    library.delete_excerpt(excerpt.id)
    assert library.excerpts() == [] and not path.exists()


# ---- pages ----------------------------------------------------------------------------

def test_study_table_page_lists_and_opens_pins(qtbot, library):
    book = add_book(library)
    table = library.add_table("Session 12")
    library.add_pin(table.id, book.id, 3, (1, 2, 3, 4), "Boss", png())
    page = StudyTablePage(library)
    qtbot.addWidget(page)
    page.show_table(table.id)
    assert page.cards.count() == 1
    assert page.cards.item(0).text() == "Boss\nTome 1 · p. 4"
    with qtbot.waitSignal(page.openRegion) as opened:
        page._open(page.cards.item(0))
    assert opened.args == [book.id, 3, (1, 2, 3, 4)]


def test_commonplace_book_lists_excerpts(qtbot, library):
    book = add_book(library)
    library.add_excerpt(png(), book.id, 2, None, "A passage")
    library.add_excerpt(png(), None, None, None, "")
    page = CommonplaceBook(library)
    qtbot.addWidget(page)
    texts = sorted(page.cards.item(i).text() for i in range(page.cards.count()))
    assert texts == ["A passage\nTome 1 · p. 3", "Source no longer in the library"]


def test_sidebar_shows_tables_and_commonplace(qtbot, library):
    importer = Importer(library)
    page = LibraryPage(library, importer)
    qtbot.addWidget(page)
    page.show()
    table = library.add_table("Session 12")
    library.add_excerpt(png())
    qtbot.wait(30)
    assert page.tree._find(("table", table.id)).text(0) == "Session 12"
    assert page.tree._find(("virtual", "commonplace")).text(1) == "1"
    page.show_key(("table", table.id))
    assert page.stack.currentWidget() is page.table_page
    assert page.heading.text() == "Session 12" and "0 pins" in page.subheading.text()
    page.show_key(("virtual", "commonplace"))
    assert page.stack.currentWidget() is page.commonplace
    page.tree._find(("table", table.id)).setText(0, "Session 13")
    assert library.table(table.id).name == "Session 13"
    importer.close()


# ---- reader: excerpt tool and pinning ---------------------------------------------------

def open_ready(qtbot, window, book):
    pane = window.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    return pane


def test_excerpt_tool_draws_a_region_and_keeps_it(qtbot, library, window):
    book = add_book(library, pdf=True)
    pane = open_ready(qtbot, window, book)
    view = pane.view
    view.set_zoom(1.0)
    qtbot.waitUntil(view.is_idle, timeout=15000)
    pane.toggle_excerpt_tool()
    assert view.tool == "excerpt"
    view.excerptDrawn.disconnect(pane._excerpt_menu)  # the pop-up menu would wait for a click
    item = view._items[0]
    def at(x, y):
        return view.mapFromScene(item.pos() + QPointF(x, y))
    with qtbot.waitSignal(view.excerptDrawn, timeout=5000) as drawn:
        qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=at(50, 140))
        qtbot.mouseMove(view.viewport(), pos=at(120, 200))
        qtbot.mouseMove(view.viewport(), pos=at(350, 320))
        qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=at(350, 320))
    page, rect, _pos = drawn.args
    assert page == 0 and rect.width() == pytest.approx(300, abs=3)
    assert not view.has_selection()  # the excerpt tool does not select text

    pane._clip("keep", page, rect)
    qtbot.waitUntil(lambda: len(library.excerpts()) == 1, timeout=15000)
    excerpt = library.excerpts()[0]
    assert excerpt.book_id == book.id and excerpt.page == 0
    saved = QImage(str(library.excerpt_file(excerpt)))
    assert saved.width() > 800  # rendered sharply, not a screen grab

    pane._clip("copy", page, QRectF(50, 140, 100, 50))
    qtbot.waitUntil(lambda: not pane._clips, timeout=15000)
    assert QApplication.clipboard().image().width() >= 300


def test_pinning_a_page_and_opening_the_pin(qtbot, library, window):
    book = add_book(library, pdf=True)
    table = library.add_table("Session 12")
    pane = open_ready(qtbot, window, book)
    pane.view.go_to_page(3)
    qtbot.waitUntil(lambda: pane.view.current_page() == 3, timeout=5000)
    pane._pin_page(table.id)
    qtbot.waitUntil(lambda: len(library.pins(table.id)) == 1, timeout=15000)
    pin = library.pins(table.id)[0]
    assert (pin.page, pin.label, pin.rect) == (3, "Second-A", None)
    assert pin.thumb and pin.thumb[:2] == b"\xff\xd8"  # a JPEG thumbnail

    pane.view.go_to_page(0)
    window.open_book_region(book.id, 3, (50, 140, 350, 320))
    qtbot.waitUntil(lambda: pane.view.current_page() == 3, timeout=5000)
    assert pane.view.marquee[0] == 3


# ---- side by side -------------------------------------------------------------------------

def test_tomes_can_sit_side_by_side_and_share_a_session(qtbot, library, window):
    one, two = add_book(library, 1, pdf=True), add_book(library, 2, pdf=True)
    first = open_ready(qtbot, window, one)
    second = open_ready(qtbot, window, two)
    assert window.right.isHidden() and window.left.count() == 3

    window.move_to_other_side()  # the second tome goes right
    assert not window.right.isHidden()
    assert window.right.widget(0) is second and window.left.count() == 2
    assert window.current_pane() is second

    window._focus_pane(first)
    window.open_alongside()  # the first tome, shown again on the right
    twin = window.right.widget(1)
    assert twin is not first and twin.book_id == one.id
    assert twin.session is first.session  # one helper process serves both views
    qtbot.waitUntil(twin.view.is_idle, timeout=15000)
    assert len(twin.view.tiles) > 0

    window.close_pane(first)
    assert twin.session.info is not None  # still alive for the remaining view
    twin.view.set_zoom(1.5)
    qtbot.waitUntil(twin.view.is_idle, timeout=15000)
    window.close_pane(twin)
    window.close_pane(second)
    assert window.right.isHidden() and window.left.count() == 1
    assert window.current_pane() is None
