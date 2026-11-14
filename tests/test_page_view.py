import pytest
from PySide6.QtGui import QImage

from bookwyrm.pdf.session_client import DocumentSession
from bookwyrm.ui.reader.page_view import PageView, TileCache
from bookwyrm.ui.reader.reader_pane import ReaderPane


@pytest.fixture
def pane(qtbot, sample_pdf):
    session = DocumentSession(str(sample_pdf))
    pane = ReaderPane(session)
    qtbot.addWidget(pane)
    pane.resize(900, 700)
    pane.show()
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    yield pane
    session.close()


def test_tile_cache_evicts_least_recently_used():
    def image():
        return QImage(100, 100, QImage.Format.Format_RGB32)  # 40 000 bytes
    cache = TileCache(budget=100_000)
    cache.put("a", image())
    cache.put("b", image())
    cache.get("a")  # "b" is now the oldest
    cache.put("c", image())
    assert "a" in cache and "c" in cache and "b" not in cache
    assert cache.bytes == 80_000


def test_view_loads_and_paints_pages(pane):
    view = pane.view
    assert view.page_count() == 6
    assert view.zoom_mode() == "fit_width"
    assert len(view.tiles) > 0
    shot = view.grab().toImage()
    centre = shot.pixelColor(shot.width() // 2, shot.height() // 2)
    assert centre != view.backgroundBrush().color()


def test_zoom_and_navigation(pane, qtbot):
    view = pane.view
    view.set_zoom(2.0)
    qtbot.waitUntil(view.is_idle, timeout=15000)
    assert view.zoom() == pytest.approx(2.0)
    assert view.zoom_mode() == "custom"
    view.zoom_in()
    assert view.zoom() == pytest.approx(2.5)
    view.zoom_out()
    view.zoom_out()
    assert view.zoom() == pytest.approx(1.75)
    view.go_to_page(4)
    qtbot.waitUntil(lambda: view.current_page() == 4, timeout=5000)
    view.step_page(-1)
    qtbot.waitUntil(lambda: view.current_page() == 3, timeout=5000)


def test_zoom_is_clamped(pane):
    pane.view.set_zoom(100)
    assert pane.view.zoom() == 8.0
    pane.view.set_zoom(0.001)
    assert pane.view.zoom() == 0.1


def test_fit_page_shows_a_whole_page(pane, qtbot):
    view = pane.view
    view.fit_page()
    qtbot.waitUntil(view.is_idle, timeout=15000)
    page = view._items[view.current_page()]
    on_screen = view.mapFromScene(page.sceneBoundingRect()).boundingRect()
    assert on_screen.height() <= view.viewport().height()


def test_state_round_trip(pane, qtbot):
    pane.view.set_zoom(1.5)
    pane.view.go_to_page(2)
    qtbot.waitUntil(lambda: pane.view.current_page() == 2, timeout=5000)
    state = pane.state()
    pane.view.fit_width()
    pane.view.go_to_page(0)
    pane.restore(state)
    qtbot.waitUntil(lambda: pane.view.current_page() == 2, timeout=5000)
    assert pane.view.zoom() == pytest.approx(1.5)


def test_view_without_session_is_inert(qtbot):
    view = PageView()
    qtbot.addWidget(view)
    view.zoom_in()
    view.go_to_page(3)
    assert view.page_count() == 0
