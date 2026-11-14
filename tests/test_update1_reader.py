"""Reader features added in the first update: Back/Forward, links, printed page numbers,
two-page spreads, full screen, printing and picture resize handles."""

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtPrintSupport import QPrinter

from bookwyrm.db.repo import Library
from bookwyrm.pdf.session_client import DocumentSession
from bookwyrm.ui import prefs
from bookwyrm.ui.main_window import MainWindow
from bookwyrm.ui.reader.page_view import PageView
from bookwyrm.ui.reader.printing import PrintJob, pages_to_print
from bookwyrm.ui.reader.reader_pane import ReaderPane

LABELS = ["i", "ii", "1", "2", "3", "4", "5", "6"]


def make_rulebook(path):
    """Eight pages: two numbered i-ii, six numbered 1-6; a link on the first page to
    'page 4' (the sixth sheet), and a web link."""
    doc = pymupdf.open()
    for number in range(8):
        page = doc.new_page(width=400, height=600)
        page.insert_text((50, 80), f"Sheet {number + 1}, printed {LABELS[number]}", fontsize=16)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(50, 200, 250, 220),
                        "page": 5, "to": pymupdf.Point(0, 300)})
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 240, 250, 260),
                        "uri": "https://example.org/errata"})
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                         {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1}])
    doc.set_toc([[1, "Front matter", 1], [1, "Chapter One", 3]])
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def pane(qtbot, tmp_path):
    session = DocumentSession(str(make_rulebook(tmp_path / "rulebook.pdf")))
    pane = ReaderPane(session)
    qtbot.addWidget(pane)
    pane.resize(1100, 760)
    pane.show()
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    pane.view.set_zoom(1.0)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    yield pane
    pane.detach_library()
    session.close()


def at(view, page, x, y):
    return view.mapFromScene(view._items[page].pos() + QPointF(x, y))


# ---- Back and Forward ------------------------------------------------------------------------

def test_back_and_forward_retrace_jumps(pane, qtbot):
    view = pane.view
    assert not pane._back_button.isEnabled() and not pane._forward_button.isEnabled()
    view.jump_to(5)
    assert view.current_page() == 5 and pane._back_button.isEnabled()
    view.jump_to(2)
    view.go_back()
    assert view.current_page() == 5 and pane._forward_button.isEnabled()
    view.go_back()
    assert view.current_page() == 0 and not pane._back_button.isEnabled()
    view.go_forward()
    view.go_forward()
    assert view.current_page() == 2 and not pane._forward_button.isEnabled()
    view.go_back()
    view.jump_to(7)  # a new jump forgets the way forward
    assert not view.can_go_forward()
    pane.side.outline.pageRequested.emit(2)  # the contents panel records history too
    view.go_back()
    assert view.current_page() == 7


# ---- links -----------------------------------------------------------------------------------

def test_clicking_a_link_jumps_and_back_returns(pane, qtbot):
    view = pane.view
    qtbot.waitUntil(lambda: 0 in view.links, timeout=15000)
    assert len(view.links[0]) == 2
    view._hover(at(view, 0, 100, 210))
    assert view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    view._hover(at(view, 0, 100, 400))
    assert view.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor

    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=at(view, 0, 100, 210))
    assert view.current_page() == 5
    assert view.page_offset() > 200  # landed part-way down the page, where the link points
    view.go_back()
    assert view.current_page() == 0

    # dragging across a link selects text instead of following it
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=at(view, 0, 60, 210))
    qtbot.mouseMove(view.viewport(), pos=at(view, 0, 120, 212))
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=at(view, 0, 200, 214))
    assert view.current_page() == 0


def test_web_links_ask_before_opening(pane, qtbot, monkeypatch):
    opened, asked = [], []
    monkeypatch.setattr("bookwyrm.ui.reader.reader_pane.QDesktopServices.openUrl",
                        lambda url: opened.append(url.toString()))
    monkeypatch.setattr("bookwyrm.ui.reader.reader_pane.dialogs.confirm",
                        lambda parent, title, message, action: asked.append(message) or True)
    pane.follow_link({"uri": "https://example.org/errata"})
    assert opened == ["https://example.org/errata"] and "example.org" in asked[0]
    with qtbot.waitSignal(pane.notice):
        pane.follow_link({"uri": "file:///etc/passwd"})  # only web and mail addresses open
    assert len(opened) == 1


# ---- printed page numbers --------------------------------------------------------------------

def test_printed_page_numbers_are_shown_and_understood(pane, qtbot):
    view = pane.view
    assert pane.page_text(0) == "i" and pane.page_text(4) == "3"
    assert pane._page_field.text() == "i" and pane._page_total.text() == "(1 of 8)"
    assert "#" in pane._page_field.toolTip()
    assert pane.page_for("3") == 4  # the page printed "3"
    assert pane.page_for("#3") == 2  # the third sheet of the file
    assert pane.page_for("II") == 1
    assert pane.page_for("7") == 6  # no page is printed 7, so it means the seventh sheet
    assert pane.page_for("99") is None and pane.page_for("chapter") is None

    pane._page_field.setText("4")
    pane._page_typed()
    assert view.current_page() == 5 and pane._page_total.text() == "(6 of 8)"
    assert view.can_go_back()
    with qtbot.waitSignal(pane.notice) as said:
        pane._page_field.setText("99")
        pane._page_typed()
    assert "no page" in said.args[0] and pane._page_field.text() == "4"

    captions = [pane.side.pages.item(i).text() for i in range(8)]
    assert captions == LABELS


def test_tomes_without_printed_numbers_use_plain_counting(qtbot, sample_pdf):
    session = DocumentSession(str(sample_pdf))
    pane = ReaderPane(session)
    qtbot.addWidget(pane)
    pane.show()
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    assert pane.page_text(3) == "4" and pane._page_total.text() == "of 6"
    assert pane.page_for("4") == 3 and pane._page_field.toolTip() == "Go to page"
    session.close()


def test_ribbons_show_printed_numbers(pane, qtbot, tmp_path):
    library = Library(tmp_path / "lib")
    book = library.add_book(title="T", author="", filename="t.pdf", sha256="h",
                            page_count=8, file_size=1)
    pane.attach_library(library, book.id)
    library.add_ribbon(book.id, 4, "Boss fight")
    assert pane.side.ribbons.list.item(0).text() == "Boss fight  ·  p. 3"
    pane.detach_library()
    library.close()


# ---- two-page spread -------------------------------------------------------------------------

def test_spread_puts_facing_pages_side_by_side(pane, qtbot):
    view = pane.view
    view.go_to_page(3)
    qtbot.waitUntil(lambda: view.current_page() == 3, timeout=5000)
    tiles_before = len(view.tiles)
    single_fit = view._fit_factor("fit_width")
    view.set_spread(True)
    cover, left, right = (view._items[i].pos() for i in (0, 1, 2))
    assert left.y() == right.y() and left.x() < right.x()  # pages 2 and 3 face each other
    assert cover.x() == right.x() and cover.y() < left.y()  # the cover stands alone, right
    assert view._fit_factor("fit_width") < single_fit * 0.6
    assert view.current_page() == 3  # still where we were (the row of pages 4-5 starts at 3)
    assert len(view.tiles) >= tiles_before > 0  # nothing had to be thrown away

    # a point on the right-hand page is recognised as that page
    page, point = view._page_point(at(view, 2, 30, 40))
    assert page == 2 and point.x() == pytest.approx(30, abs=1)
    view.go_to_page(1)
    view._track_now()
    view.step_page(1)  # the next pair, not the page beside this one
    view._track_now()
    assert view.current_page() == 3
    view.step_page(-1)
    view._track_now()
    assert view.current_page() in (1, 2)
    view.set_spread(False)
    assert view._items[1].pos().y() < view._items[2].pos().y()


def test_spread_is_a_remembered_preference(pane):
    assert not pane.view.spread
    pane.toggle_spread()
    assert pane.view.spread and prefs.get("reader/spread") is True
    prefs.put("reader/spread", False)
    pane.apply_preferences()
    assert not pane.view.spread


# ---- full screen -----------------------------------------------------------------------------

def test_full_screen_hides_the_trimmings_and_escape_brings_them_back(qtbot, tmp_path):
    library = Library(tmp_path / "lib")
    window = MainWindow(library)
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    window.toggle_full_screen()  # nothing to read on the Library tab: stays as it is
    assert not window.isFullScreen()
    pane = window.open_path(str(make_rulebook(tmp_path / "r.pdf")))
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    window.toggle_full_screen()
    assert window.isFullScreen()
    assert pane.toolbar.isHidden() and pane.side.isHidden()
    assert window.left.tabBar().isHidden() and window.statusBar().isHidden()
    window.leave_full_screen()
    assert not window.isFullScreen()
    assert not pane.toolbar.isHidden() and not window.left.tabBar().isHidden()
    assert pane.side.isHidden() != prefs.get("reader/side_panel")
    window.close()
    library.close()


# ---- printing --------------------------------------------------------------------------------

def pdf_printer(path):
    printer = QPrinter(QPrinter.PrinterMode.ScreenResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    return printer


def test_printing_sends_the_chosen_pages(pane, qtbot, tmp_path):
    out = tmp_path / "printed.pdf"
    printer = pdf_printer(out)
    job = PrintJob(pane.session, printer, [0, 2, 4])
    progress = []
    job.progressed.connect(lambda done, total: progress.append((done, total)))
    with qtbot.waitSignal(job.finished, timeout=30000) as finished:
        job.start()
    assert finished.args == [True, ""] and progress == [(1, 3), (2, 3), (3, 3)]
    printed = pymupdf.open(out)
    assert printed.page_count == 3
    printed.close()


def test_printing_can_be_cancelled_and_empty_jobs_are_refused(pane, qtbot, tmp_path):
    job = PrintJob(pane.session, pdf_printer(tmp_path / "a.pdf"), list(range(8)))
    with qtbot.waitSignal(job.finished, timeout=30000) as finished:
        job.start()
        job.cancel()
    assert finished.args[0] is False and "cancelled" in finished.args[1]
    empty = PrintJob(pane.session, pdf_printer(tmp_path / "b.pdf"), [])
    with qtbot.waitSignal(empty.finished, timeout=5000) as finished:
        empty.start()
    assert finished.args[0] is False


def test_page_ranges_from_the_print_dialog(qapp):
    printer = QPrinter()
    assert pages_to_print(printer, 8, 3) == list(range(8))
    printer.setPrintRange(QPrinter.PrintRange.CurrentPage)
    assert pages_to_print(printer, 8, 3) == [3]
    printer.setPrintRange(QPrinter.PrintRange.PageRange)
    printer.setFromTo(2, 5)
    assert pages_to_print(printer, 8, 3) == [1, 2, 3, 4]
    printer.setFromTo(6, 99)
    assert pages_to_print(printer, 8, 3) == [5, 6, 7]


# ---- picture resize handles ------------------------------------------------------------------

def test_resizing_keeps_proportions_unless_asked_not_to():
    box = QRectF(100, 100, 200, 100)  # twice as wide as tall
    grown = PageView._resized(box, "br", QPointF(500, 150), free=False)
    assert grown.topLeft() == box.topLeft()  # the opposite corner stays put
    assert grown.width() == pytest.approx(400) and grown.height() == pytest.approx(200)
    free = PageView._resized(box, "br", QPointF(500, 150), free=True)
    assert (free.width(), free.height()) == (400, 50)
    from_top_left = PageView._resized(box, "tl", QPointF(200, 150), free=False)
    assert from_top_left.bottomRight() == box.bottomRight()
    assert from_top_left.width() == pytest.approx(100)
    assert from_top_left.height() == pytest.approx(50)
    tiny = PageView._resized(box, "br", QPointF(0, 0), free=True)
    assert tiny.width() == 8 and tiny.height() == 8  # never collapses to nothing


def test_dragging_a_corner_handle_resizes_the_selected_picture(pane, qtbot):
    view = pane.view
    view.set_tool("pointer")
    view.resizable = True
    view.set_object_boxes(0, [(7, (100, 300, 200, 350), True)])
    view.select_object((0, 7))
    assert view._handle_at(0, QPointF(200, 350)) == "br"
    assert view._handle_at(0, QPointF(150, 325)) is None
    view._hover(at(view, 0, 200, 350))
    assert view.viewport().cursor().shape() == Qt.CursorShape.SizeFDiagCursor

    with qtbot.waitSignal(view.objectMoved) as moved:
        qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=at(view, 0, 200, 350))
        qtbot.mouseMove(view.viewport(), pos=at(view, 0, 240, 370))
        qtbot.mouseMove(view.viewport(), pos=at(view, 0, 300, 400))
        qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=at(view, 0, 300, 400))
    page, ident, box = moved.args
    assert (page, ident) == (0, 7) and box.topLeft() == QPointF(100, 300)
    assert box.width() == pytest.approx(200, abs=3) and box.height() == pytest.approx(100, abs=3)

    view.resizable = False  # notes and shapes are moved, not resized
    assert view._handle_at(0, QPointF(200, 350)) is None
