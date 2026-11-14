import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication

from bookwyrm.db.repo import Library
from bookwyrm.pdf.session_client import DocumentSession
from bookwyrm.ui.extras.card_catalogue import CardCatalogue, find_needle, snippet_html
from bookwyrm.ui.reader import selection
from bookwyrm.ui.reader.reader_pane import ReaderPane

WORDS = [  # x0, y0, x1, y1, text, block, line, n
    (10, 10, 40, 20, "The", 0, 0, 0), (45, 10, 90, 20, "silver", 0, 0, 1),
    (95, 10, 140, 20, "dragon", 0, 0, 2), (10, 25, 50, 35, "guards", 0, 1, 0),
    (55, 25, 80, 35, "its", 0, 1, 1), (10, 60, 60, 70, "Hoard.", 1, 0, 0),
]


def test_nearest_word_prefers_the_right_line():
    assert selection.nearest_word(WORDS, 50, 15) == 1  # inside "silver"
    assert selection.nearest_word(WORDS, 200, 27) == 4  # right of line two -> "its"
    assert selection.nearest_word(WORDS, 0, 100) == 5
    assert selection.nearest_word([], 1, 1) is None


def test_word_at_needs_a_real_hit():
    assert selection.word_at(WORDS, 100, 15) == 2
    assert selection.word_at(WORDS, 200, 200) is None


def test_line_rects_merge_words_on_a_line():
    assert selection.line_rects(WORDS[1:5]) == [(45, 10, 140, 20), (10, 25, 80, 35)]


def test_text_of_keeps_lines_and_blocks():
    assert selection.text_of(WORDS) == "The silver dragon\nguards its\n\nHoard."


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture
def pane(qtbot, sample_pdf, monkeypatch):
    session = DocumentSession(str(sample_pdf))
    pane = ReaderPane(session)
    qtbot.addWidget(pane)
    pane.resize(1100, 760)
    pane.show()
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    yield pane
    pane.detach_library()
    session.close()


def test_find_highlights_and_steps_through_matches(pane, qtbot):
    pane.open_find("keyword")
    qtbot.waitUntil(lambda: pane.find.status.text() == "1 of 6", timeout=15000)
    assert sorted(pane.view.hits) == [0, 1, 2, 3, 4, 5]
    assert pane.view.current_hit == (0, 0)
    pane.step_match(1)
    assert pane.view.current_hit == (1, 0)
    assert pane.find.status.text() == "2 of 6"
    pane.step_match(-1)
    pane.step_match(-1)  # wraps to the last match
    assert pane.view.current_hit == (5, 0)
    qtbot.waitUntil(lambda: pane.view.current_page() == 5, timeout=5000)
    pane.find.close_bar()
    assert pane.view.hits == {} and pane.view.current_hit is None


def test_find_reports_no_matches(pane, qtbot):
    pane.open_find("zzzqqq")
    qtbot.waitUntil(lambda: pane.find.status.text() == "No matches", timeout=15000)


def test_drag_selects_text_and_copies_it(pane, qtbot):
    view = pane.view
    view.set_zoom(1.0)
    qtbot.waitUntil(view.is_idle, timeout=15000)
    item = view._items[0]
    def at(x, y):  # page point -> viewport position
        return view.mapFromScene(item.pos() + QPointF(x, y))
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=at(52, 74))
    qtbot.mouseMove(view.viewport(), pos=at(120, 76))
    qtbot.mouseMove(view.viewport(), pos=at(330, 108))
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=at(330, 108))
    qtbot.waitUntil(view.has_selection, timeout=5000)
    text = view.selected_text()
    assert text.startswith("Page 1 of the test tome")
    assert "keyword-1" in text and "silver" in text
    assert view.selection_rects[0]
    assert pane.copy_selection()
    assert QApplication.clipboard().text() == text
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert not view.has_selection()


def test_double_click_selects_one_word(pane, qtbot):
    view = pane.view
    view.set_zoom(1.0)
    qtbot.waitUntil(view.is_idle, timeout=15000)
    spot = view.mapFromScene(view._items[0].pos() + QPointF(60, 74))
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=spot)
    qtbot.waitUntil(view.has_selection, timeout=5000)
    assert view.selected_text() == "Page"


def test_outline_and_thumbnails_follow_and_jump(pane, qtbot):
    outline = pane.side.outline
    assert outline.topLevelItemCount() == 2
    pane.view.go_to_page(3)
    qtbot.waitUntil(lambda: pane.view.current_page() == 3, timeout=5000)
    assert outline.currentItem().text(0) == "Second-A"
    assert pane.side.pages.count() == 6 and pane.side.pages.currentRow() == 3
    outline.pageRequested.emit(0)
    qtbot.waitUntil(lambda: pane.view.current_page() == 0, timeout=5000)


def test_reading_modes_cycle_and_change_the_page(pane, qtbot):
    view = pane.view
    def centre():
        shot = view.grab().toImage()
        return shot.pixelColor(shot.width() // 2, 120)
    day = centre()
    pane.cycle_reading_mode()
    assert view.reading_mode == "candle"
    candle = centre()
    pane.cycle_reading_mode()
    assert view.reading_mode == "night"
    night = centre()
    assert candle.lightness() < day.lightness()
    assert night.lightness() < 110 < day.lightness()
    pane.cycle_reading_mode()
    assert view.reading_mode == "day"


def test_ribbons_toggle_and_show_on_the_page(pane, qtbot, library):
    book = library.add_book(title="T", author="", filename="t.pdf", sha256="h",
                            page_count=6, file_size=1)
    pane.attach_library(library, book.id)
    pane.view.go_to_page(3)
    qtbot.waitUntil(lambda: pane.view.current_page() == 3, timeout=5000)
    pane.toggle_ribbon()
    ribbons = library.ribbons(book.id)
    assert [(r.page, r.label) for r in ribbons] == [(3, "Second-A")]  # named after the section
    assert pane.view.ribbon_pages == {3}
    assert pane.side.ribbons.list.count() == 1
    library.rename_ribbon(ribbons[0].id, "Boss fight")
    assert pane.side.ribbons.list.item(0).text() == "Boss fight  ·  p. 4"
    pane.toggle_ribbon()
    assert library.ribbons(book.id) == [] and pane.view.ribbon_pages == set()


def test_snippet_html_escapes_and_marks():
    assert snippet_html("a <b> \x02dragon\x03\nhoard") == \
        'a &lt;b&gt; <b style="color:#e3b057">dragon</b> hoard'


@pytest.mark.parametrize("query, needle", [
    ("dragon", "dragon"), ('"silver dragon" hoard', "silver dragon"),
    ("frost breath weapon", "breath"), ("  ", ""),
])
def test_find_needle(query, needle):
    assert find_needle(query) == needle


def test_card_catalogue_lists_pages_by_tome(qtbot, library, tmp_path):
    books = []
    for n in (1, 2):
        book = library.add_book(title=f"Tome {n}", author="", filename=f"t{n}.pdf",
                                sha256=f"h{n}", page_count=3, file_size=1)
        library.mark_ready(book.id)
        library.set_indexed(book.id)
        books.append(book)
    library.add_page_text(books[0].id, [(0, "A silver dragon sleeps."), (2, "Dragons hoard.")])
    library.add_page_text(books[1].id, [(1, "No wyrms here, only a dragon turtle.")])
    catalogue = CardCatalogue(library)
    qtbot.addWidget(catalogue)
    assert "all 2 tomes" in catalogue.summary.text()
    catalogue.field.setText("dragon")
    catalogue.search()
    results = catalogue.results
    assert results.topLevelItemCount() == 2
    assert sum(results.topLevelItem(i).childCount() for i in range(2)) == 3
    assert catalogue.summary.text().startswith("3 pages in 2 tomes")
    with qtbot.waitSignal(catalogue.openRequested) as opened:
        first = results.topLevelItem(0).child(0)
        catalogue._activated(first)
    book_id, page, needle = opened.args
    assert needle == "dragon" and page in (0, 1, 2)
    catalogue.field.setText("basilisk")
    catalogue.search()
    assert "No page" in catalogue.summary.text()
