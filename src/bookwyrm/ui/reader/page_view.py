"""Continuous-scroll page view.

Scene units are PDF points; zoom is the view transform. Each page paints itself
from, in order: a low-resolution preview, tiles left over from the previous zoom
level, and crisp tiles at the current zoom level. Tiles are rendered by the
document host process and cached here under a byte budget.
"""

from __future__ import annotations

import bisect
import math
from collections import OrderedDict

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QLineEdit,
    QPlainTextEdit,
)

from bookwyrm.ui.reader import selection

PT_TO_PX = 96.0 / 72.0  # 100 % zoom shows one PDF point as 1.33 logical pixels
TILE = 768  # tile edge in device pixels
GAP = 10.0  # points between pages
GUTTER = 3.0  # points between the two pages of a spread
HISTORY = 100  # places remembered for Back
HANDLE = 7.0  # resize handle size, in screen pixels
MARGIN = 14.0  # points around the page column
MIN_ZOOM, MAX_ZOOM = 0.1, 8.0
ZOOM_STEPS = (0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75,
              2.0, 2.5, 3.0, 4.0, 5.0, 6.4, 8.0)
PREVIEW_WIDTH = 200  # pixels
PREVIEW_LIMIT = 240  # previews kept in memory
TILE_BUDGET = 320 * 1024 * 1024  # bytes of tiles kept in memory
PAGE_COLOR = QColor("#f4efe4")
WORD_CACHE = 60  # pages whose word boxes are kept for text selection

READING_MODES = ("day", "candle", "night")
CANDLE_TINT = QColor(247, 221, 172)  # multiplied over the page: warm, dimmer whites
NIGHT_TINT = QColor(214, 220, 232)  # softens the inverted page
SELECTION_COLOR = QColor(111, 191, 227, 95)
HIT_COLOR = QColor(255, 204, 64, 110)
CURRENT_HIT_COLOR = QColor(255, 140, 20, 150)
RIBBON_COLOR = QColor("#b3262e")
FIELD_COLOR = QColor(111, 191, 227, 38)
OBJECT_COLOR = QColor("#6fbfe3")

# How each tool uses the mouse.
TEXT_TOOLS = frozenset({"select", "highlight", "underline", "strikeout"})  # drag over text
BOX_TOOLS = frozenset({"excerpt", "textbox", "rect", "ellipse", "erase", "add_image"})
LINE_TOOLS = frozenset({"line", "arrow"})  # drag from one point to another
CLICK_TOOLS = frozenset({"note", "form", "edit_text", "add_text"})  # click a spot
# "ink" (freehand) and "pointer" (select and move things) have their own handling.


class TileCache:
    """Least-recently-used image cache bounded by total bytes."""

    def __init__(self, budget: int):
        self.budget = budget
        self._items: OrderedDict[tuple, QImage] = OrderedDict()
        self._bytes = 0

    def __contains__(self, key) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    @property
    def bytes(self) -> int:
        return self._bytes

    def get(self, key):
        image = self._items.get(key)
        if image is not None:
            self._items.move_to_end(key)
        return image

    def put(self, key, image: QImage) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old.sizeInBytes()
        self._items[key] = image
        self._bytes += image.sizeInBytes()
        while self._bytes > self.budget and len(self._items) > 1:
            _, dropped = self._items.popitem(last=False)
            self._bytes -= dropped.sizeInBytes()

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0


class PageItem(QGraphicsItem):
    def __init__(self, view: PageView, index: int, width: float, height: float):
        super().__init__()
        self._view = view
        self.index = index
        self.rect = QRectF(0, 0, width, height)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def boundingRect(self) -> QRectF:
        return self.rect

    def grid(self, scale: float) -> tuple[float, int, int]:
        """(cell size in points, columns, rows) of the tile grid at `scale`."""
        cell = TILE / scale
        cols = max(1, math.ceil(self.rect.width() / cell - 1e-6))
        rows = max(1, math.ceil(self.rect.height() / cell - 1e-6))
        return cell, cols, rows

    def cells(self, area: QRectF, scale: float):
        """Yield (col, row) for every tile touching `area` (page coordinates)."""
        if area.isEmpty():
            return
        cell, cols, rows = self.grid(scale)
        c0 = max(0, int(area.left() // cell))
        c1 = min(cols - 1, int((area.right() - 1e-6) // cell))
        r0 = max(0, int(area.top() // cell))
        r1 = min(rows - 1, int((area.bottom() - 1e-6) // cell))
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                yield col, row

    def cell_rect(self, col: int, row: int, scale: float) -> QRectF:
        cell = TILE / scale
        return QRectF(col * cell, row * cell, cell, cell) & self.rect

    def paint(self, painter: QPainter, option, widget=None) -> None:
        view = self._view
        painter.fillRect(self.rect, PAGE_COLOR)
        preview = view.previews.get(self.index)
        if preview is not None:
            painter.drawImage(self.rect, preview)
        exposed = option.exposedRect & self.rect
        for scale in view.paint_scales():
            cell = TILE / scale
            for revision in view.paint_revs(self.index):
                for col, row in self.cells(exposed, scale):
                    image = view.tiles.get((revision, self.index, scale, col, row))
                    if image is not None:
                        target = QRectF(col * cell, row * cell,
                                        image.width() / scale, image.height() / scale)
                        painter.drawImage(target, image)
        self._paint_overlays(painter, view)
        self._paint_editing(painter, view)

    def _paint_editing(self, painter: QPainter, view) -> None:
        """Form fields, selectable objects and whatever is being drawn right now."""
        fields = view.field_boxes.get(self.index)
        if fields:
            thin = QPen(OBJECT_COLOR, 0)
            painter.setPen(thin)
            painter.setBrush(FIELD_COLOR)
            for box in fields:
                painter.drawRect(box)
        if view.tool == "pointer":
            selected = view.selected_object
            for xref, box, _movable in view.object_boxes.get(self.index, ()):
                chosen = selected == (self.index, xref)
                pen = QPen(OBJECT_COLOR, 0)
                pen.setStyle(Qt.PenStyle.SolidLine if chosen else Qt.PenStyle.DotLine)
                painter.setPen(pen)
                painter.setBrush(QColor(111, 191, 227, 50) if chosen else Qt.BrushStyle.NoBrush)
                painter.drawRect(box.adjusted(-2, -2, 2, 2))
                if chosen and view.resizable:
                    half = HANDLE / 2 / (view.zoom() * PT_TO_PX)
                    painter.setPen(QPen(QColor("#10141c"), 0))
                    painter.setBrush(OBJECT_COLOR)
                    for corner in (box.topLeft(), box.topRight(), box.bottomLeft(),
                                   box.bottomRight()):
                        painter.drawRect(QRectF(corner.x() - half, corner.y() - half,
                                                2 * half, 2 * half))
        preview = view.preview
        if preview is None or preview[1] != self.index:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(view.pen_color, view.pen_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        kind = preview[0]
        if kind == "line":
            painter.drawLine(preview[2], preview[3])
        elif kind == "ink" and len(preview[2]) > 1:
            path = QPainterPath(preview[2][0])
            for point in preview[2][1:]:
                path.lineTo(point)
            painter.drawPath(path)
        elif kind == "box":
            dashed = QPen(OBJECT_COLOR, 0)
            dashed.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(dashed)
            painter.drawRect(preview[2])

    def _paint_overlays(self, painter: QPainter, view) -> None:
        mode = view.reading_mode
        if mode == "candle":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.fillRect(self.rect, CANDLE_TINT)
        elif mode == "night":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
            painter.fillRect(self.rect, Qt.GlobalColor.white)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.fillRect(self.rect, NIGHT_TINT)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        hits = view.hits.get(self.index)
        if hits:
            current = view.current_hit
            for number, rect in enumerate(hits):
                is_current = current == (self.index, number)
                painter.fillRect(rect, CURRENT_HIT_COLOR if is_current else HIT_COLOR)
        for rect in view.selection_rects.get(self.index, ()):
            painter.fillRect(rect, SELECTION_COLOR)
        if view.marquee is not None and view.marquee[0] == self.index:
            box = view.marquee[1]
            painter.fillRect(box, QColor(111, 191, 227, 45))
            pen = QPen(QColor("#a9dcf2"), 0)  # cosmetic: one device pixel at any zoom
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box)
        if self.index in view.ribbon_pages:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            x = self.rect.right() - 30
            painter.setPen(QPen(QColor(0, 0, 0, 90), 0.5))
            painter.setBrush(RIBBON_COLOR)
            painter.drawPolygon(QPolygonF([
                QPointF(x, 0), QPointF(x + 14, 0), QPointF(x + 14, 30),
                QPointF(x + 7, 23), QPointF(x, 30),
            ]))


class PageView(QGraphicsView):
    currentPageChanged = Signal(int)  # 0-based
    zoomChanged = Signal(float, str)  # factor (1.0 = 100 %), mode
    loaded = Signal()
    idle = Signal()  # nothing left to fetch for what is on screen
    selectionChanged = Signal(bool)  # is any text selected?
    excerptDrawn = Signal(int, QRectF, QPoint)  # page, region (page points), global position
    # editing gestures; coordinates are page points
    # `object`, not `dict`: Qt would try to turn a dict into a string-keyed map and fail
    markupDrawn = Signal(str, object)  # tool, {page: [(x0, y0, x1, y1), ...]} line boxes
    boxDrawn = Signal(str, int, QRectF, QPoint)  # tool, page, box, global position
    lineDrawn = Signal(str, int, QPointF, QPointF)  # tool, page, from, to
    inkDrawn = Signal(int, list)  # page, [(x, y), ...]
    pageClicked = Signal(str, int, QPointF, QPoint)  # tool, page, point, global position
    objectSelected = Signal(object)  # (page, xref) | None
    objectMoved = Signal(int, int, QRectF)  # page, xref, new box
    objectActivated = Signal(int, int)  # page, xref: double-clicked
    objectDeleteRequested = Signal(int, int)  # page, xref
    inlineTabbed = Signal(bool)  # Tab (False) or Shift+Tab (True) left an inline editor
    linkActivated = Signal(object)  # {"page": int, "y": float} or {"uri": str}
    historyChanged = Signal(bool, bool)  # can go back, can go forward

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setBackgroundBrush(QColor("#161c28"))

        self.tiles = TileCache(TILE_BUDGET)
        self.previews: OrderedDict[int, QImage] = OrderedDict()
        self._session = None
        self._items: list[PageItem] = []
        self._tops: list[float] = []
        self._max_w = 1.0
        self._zoom = 1.0
        self._mode = "fit_width"
        self._scale = 0.0  # settled render scale, device pixels per point
        self._stale_scale: float | None = None
        self._inflight: dict[tuple, tuple[int, int]] = {}  # key -> (req_id, priority)
        self._keys: dict[int, tuple] = {}  # req_id -> key
        self._failed: set[tuple] = set()
        self._current = -1

        # overlays painted on top of the pages
        self.reading_mode = "day"
        self.hits: dict[int, list[QRectF]] = {}  # find-in-document matches
        self.current_hit: tuple[int, int] | None = None  # (page, index into hits[page])
        self.selection_rects: dict[int, list[QRectF]] = {}
        self.ribbon_pages: set[int] = set()
        # text selection
        self._words: OrderedDict[int, list] = OrderedDict()
        self._word_requests: dict[int, int] = {}  # req_id -> page
        self._selected: dict[int, list] = {}  # page -> selected words
        self._anchor: tuple[int, QPointF] | None = None
        self._focus: tuple[int, QPointF] | None = None
        self._press_pos: QPoint | None = None
        self._selecting = False
        self._pending_markup: str | None = None  # a markup drag waiting for word boxes
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        # tools
        self.tool = "select"
        self.marquee: tuple[int, QRectF] | None = None
        # what is being drawn: ("line", page, a, b) | ("ink", page, points) | ("box", page, rect)
        self.preview: tuple | None = None
        self.pen_color = QColor("#cc2020")
        self.pen_width = 2.0
        self.object_boxes: dict[int, list[tuple[int, QRectF, bool]]] = {}  # xref, box, movable
        self.field_boxes: dict[int, list[QRectF]] = {}
        self.selected_object: tuple[int, int] | None = None
        self._grab: tuple | None = None  # (page, xref, box, press point) while moving an object
        self._inline: tuple | None = None  # (widget, callback, multiline)
        # edits repaint single pages: each page has its own revision
        self.page_revs: dict[int, int] = {}
        self._stale_revs: dict[int, int] = {}
        # layout: one page per row, or two side by side like an open book
        self.spread = False
        self._sizes: list = []
        self._row_start: list[int] = []  # for each page, the first page of its row
        self._row_end: list[int] = []  # ... and the last
        # Back and Forward
        self._back: list[tuple[int, float]] = []
        self._forward: list[tuple[int, float]] = []
        # links inside the tome
        self.links: dict[int, list[tuple[QRectF, dict]]] = {}
        self._link_requests: dict[int, int] = {}  # req_id -> page
        self._pressed_link: dict | None = None
        self.resizable = False  # may the selected object be resized by its corners?
        self.viewport().setMouseTracking(True)  # to show a hand over links

        self._refresh_timer = QTimer(self, singleShot=True, interval=0)
        self._refresh_timer.timeout.connect(self._refresh)
        self._settle_timer = QTimer(self, singleShot=True, interval=90)
        self._settle_timer.timeout.connect(self._settle)
        # Not `timer.start` directly: valueChanged(int) would be taken as the delay.
        self.verticalScrollBar().valueChanged.connect(self._schedule_refresh)
        self.horizontalScrollBar().valueChanged.connect(self._schedule_refresh)
        self._inline_filter = _InlineFilter(self)

    def _schedule_refresh(self, *_) -> None:
        if self._inline is not None:
            self.commit_inline()  # an inline editor cannot follow a scroll or zoom
        self._refresh_timer.start(0)

    # -- document ---------------------------------------------------------

    def set_session(self, session) -> None:
        self._session = session
        session.pixels.connect(self._on_pixels)
        session.failed.connect(self._on_failed)
        session.reply.connect(self._on_reply)
        if session.info is not None:
            self._build(session.info)
        else:
            session.opened.connect(self._build)

    def page_count(self) -> int:
        return len(self._items)

    def _build(self, info: dict) -> None:
        self._lay_out(info["sizes"])
        if self._mode in ("fit_width", "fit_page"):
            self.set_zoom(self._fit_factor(self._mode), self._mode)
        else:
            self.set_zoom(self._zoom, self._mode)
        self._settle()
        self.loaded.emit()

    def _lay_out(self, sizes: list) -> None:
        """(Re)create the page items. Everything cached about the old pages is dropped."""
        if self._session is not None and self._inflight:
            self._session.cancel(req_id for req_id, _ in self._inflight.values())
        self.commit_inline(False)
        self.tiles.clear()
        self.previews.clear()
        self._inflight.clear()
        self._keys.clear()
        self._failed.clear()
        self._words.clear()
        self._word_requests.clear()
        self.links.clear()
        self._link_requests.clear()
        self._selected, self.selection_rects = {}, {}
        self._anchor = self._focus = None
        self.hits, self.current_hit = {}, None
        self.page_revs, self._stale_revs = {}, {}
        self.object_boxes, self.field_boxes = {}, {}
        self.selected_object = self.marquee = self.preview = self._grab = None
        self._back.clear()
        self._forward.clear()
        self.historyChanged.emit(False, False)
        self._place(sizes)

    def _place(self, sizes: list) -> None:
        """Position the pages: one to a row, or as spreads. Cached pictures stay valid."""
        self._scene.clear()
        self._sizes = list(sizes)
        self._items, self._tops, self._row_start, self._row_end = [], [], [], []
        widest = max((w for w, _ in sizes), default=1.0)
        if self.spread:  # like a book: the cover alone on the right, then facing pages
            rows = [[0]] if sizes else []
            rows += [list(range(i, min(i + 2, len(sizes)))) for i in range(1, len(sizes), 2)]
            self._max_w = 2 * widest + GUTTER
        else:
            rows = [[i] for i in range(len(sizes))]
            self._max_w = widest
        spine = MARGIN + self._max_w / 2
        y = MARGIN
        for row in rows:
            height = max(sizes[i][1] for i in row)
            for position, index in enumerate(row):
                w, h = sizes[index]
                item = PageItem(self, index, w, h)
                if not self.spread:
                    x = MARGIN + (widest - w) / 2
                elif index == 0 or position == 1:  # right-hand page
                    x = spine + GUTTER / 2
                else:  # left-hand page
                    x = spine - GUTTER / 2 - w
                item.setPos(x, y + (height - h) / 2)
                self._scene.addItem(item)
                self._items.append(item)
                self._tops.append(y)
                self._row_start.append(row[0])
                self._row_end.append(row[-1])
            y += height + GAP
        self._scene.setSceneRect(0, 0, self._max_w + 2 * MARGIN, y - GAP + MARGIN)
        self._current = -1

    def set_spread(self, on: bool) -> None:
        """Show two pages side by side (True) or one page per row (False)."""
        if on == self.spread:
            return
        self.spread = on
        if not self._items:
            return
        self.commit_inline(False)
        page = self.current_page()
        self._place(self._sizes)
        if self._mode in ("fit_width", "fit_page"):
            self.set_zoom(self._fit_factor(self._mode), self._mode)
        self.go_to_page(page)
        self._settle()

    def paint_revs(self, index: int) -> tuple[int, ...]:
        """Revisions of a page worth painting: the outgoing one beneath the current one."""
        current = self.page_revs.get(index, 0)
        stale = self._stale_revs.get(index)
        return (stale, current) if stale is not None and stale != current else (current,)

    def apply_edit(self, state: dict) -> None:
        """The tome changed: repaint the touched pages, or lay everything out again."""
        if state.get("structural") and state.get("sizes") is not None:
            page, offset = self.current_page(), self.page_offset()
            self._lay_out(state["sizes"])
            if self._mode in ("fit_width", "fit_page"):
                self.set_zoom(self._fit_factor(self._mode), self._mode)
            self.go_to_page(min(page, max(0, len(self._items) - 1)), offset)
            self.loaded.emit()
        else:
            pages = state.get("changed")
            if pages is None:
                pages = range(len(self._items))
            for page in pages:
                if not 0 <= page < len(self._items):
                    continue
                old = self.page_revs.get(page, 0)
                self._stale_revs.setdefault(page, old)
                self.page_revs[page] = old + 1
                self._words.pop(page, None)
                self.links.pop(page, None)
                self.previews.pop(page, None)
                self.object_boxes.pop(page, None)
                if self.hits.pop(page, None) is not None and self.current_hit \
                        and self.current_hit[0] == page:
                    self.current_hit = None
                if page in self._selected:
                    self.clear_selection()
        self._schedule_refresh()

    # -- zoom ---------------------------------------------------------------

    def zoom(self) -> float:
        return self._zoom

    def zoom_mode(self) -> str:
        return self._mode

    def paint_scales(self) -> tuple[float, ...]:
        if self._stale_scale and self._stale_scale != self._scale:
            return (self._stale_scale, self._scale) if self._scale else (self._stale_scale,)
        return (self._scale,) if self._scale else ()

    def set_zoom(self, factor: float, mode: str = "custom", anchor: QPoint | None = None) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        self._mode = mode
        # A fitted page never needs sideways scrolling; without this a rounding pixel
        # can summon a scrollbar that then steals height.
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if mode in ("fit_width", "fit_page")
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if anchor is None:
            anchor = self.viewport().rect().center()
        scene_point = self.mapToScene(anchor)
        self._zoom = factor
        z = factor * PT_TO_PX
        self.setTransform(QTransform.fromScale(z, z))
        moved = self.mapFromScene(scene_point)
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        hbar.setValue(hbar.value() + moved.x() - anchor.x())
        vbar.setValue(vbar.value() + moved.y() - anchor.y())
        self._settle_timer.start()
        self._schedule_refresh()
        self.zoomChanged.emit(factor, mode)

    def zoom_in(self) -> None:
        self.set_zoom(next((s for s in ZOOM_STEPS if s > self._zoom * 1.01), MAX_ZOOM))

    def zoom_out(self) -> None:
        self.set_zoom(next((s for s in reversed(ZOOM_STEPS) if s < self._zoom * 0.99), MIN_ZOOM))

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def fit_width(self) -> None:
        self.set_zoom(self._fit_factor("fit_width"), "fit_width")

    def fit_page(self) -> None:
        page = max(0, self._current)
        self.set_zoom(self._fit_factor("fit_page"), "fit_page")
        self.go_to_page(page)

    def _fit_factor(self, mode: str) -> float:
        viewport = self.viewport().size()
        by_width = viewport.width() / ((self._max_w + 2 * MARGIN) * PT_TO_PX)
        if mode == "fit_width" or not self._items:
            return by_width
        current = max(0, self._current)
        page_h = max(self._items[i].rect.height()
                     for i in range(self._row_start[current], self._row_end[current] + 1))
        by_height = viewport.height() / ((page_h + MARGIN) * PT_TO_PX)
        return min(by_width, by_height)

    def _settle(self) -> None:
        """Zoom has stopped changing: start fetching tiles at the new scale."""
        self._settle_timer.stop()
        scale = round(self._zoom * PT_TO_PX * self.devicePixelRatioF(), 4)
        if scale != self._scale:
            if self._scale:
                self._stale_scale = self._scale
            self._scale = scale
        self._refresh()

    # -- navigation -----------------------------------------------------------

    def current_page(self) -> int:
        return max(0, self._current)

    def go_to_page(self, index: int, offset: float = 0.0) -> None:
        """Scroll so the top of page `index` (plus `offset` points) is at the top."""
        if not self._items:
            return
        index = max(0, min(len(self._items) - 1, index))
        y = self._tops[index] - MARGIN / 2 + offset
        delta = self.mapFromScene(QPointF(0, y)).y()
        vbar = self.verticalScrollBar()
        vbar.setValue(vbar.value() + delta)
        self._schedule_refresh()

    def page_offset(self) -> float:
        """How far (points) the viewport top sits below the current page's top."""
        if not self._items:
            return 0.0
        top = self.mapToScene(QPoint(0, 0)).y()
        return top - (self._tops[self.current_page()] - MARGIN / 2)

    def step_page(self, delta: int) -> None:
        """The next or previous page (or, in a spread, the next or previous pair)."""
        page = self.current_page()
        if delta > 0:
            self.go_to_page(self._row_end[page] + delta if self._items else 0)
        else:
            self.go_to_page(self._row_start[page] + delta if self._items else 0)

    # -- Back and Forward ----------------------------------------------------------

    def _here(self) -> tuple[int, float]:
        return self.current_page(), self.page_offset()

    def jump_to(self, page: int, offset: float = 0.0) -> None:
        """Go somewhere else in the tome and remember where we were, for Back."""
        if not self._items:
            return
        here = self._here()
        if not self._back or self._back[-1] != here:
            self._back.append(here)
            del self._back[:-HISTORY]
        self._forward.clear()
        self.go_to_page(page, offset)
        self._track_now()
        self.historyChanged.emit(True, False)

    def can_go_back(self) -> bool:
        return bool(self._back)

    def can_go_forward(self) -> bool:
        return bool(self._forward)

    def go_back(self) -> None:
        if self._back:
            self._forward.append(self._here())
            self.go_to_page(*self._back.pop())
            self._track_now()
            self.historyChanged.emit(bool(self._back), True)

    def go_forward(self) -> None:
        if self._forward:
            self._back.append(self._here())
            self.go_to_page(*self._forward.pop())
            self._track_now()
            self.historyChanged.emit(True, bool(self._forward))

    def _track_now(self) -> None:
        """Bring the 'current page' up to date at once (it normally follows on a timer)."""
        self._track_current(self.mapToScene(self.viewport().rect()).boundingRect())

    # -- fetching ---------------------------------------------------------------

    def _pages_in(self, area: QRectF) -> range:
        if not self._items:
            return range(0)
        first = self._row_start[max(0, bisect.bisect_right(self._tops, area.top()) - 1)]
        last = min(len(self._items) - 1, bisect.bisect_left(self._tops, area.bottom()))
        return range(first, self._row_end[last] + 1)

    def _refresh(self) -> None:
        if not self._items or self._session is None:
            return
        view_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        self._track_current(view_rect)
        if not self._settle_timer.isActive():
            live = round(self._zoom * PT_TO_PX * self.devicePixelRatioF(), 4)
            if live != self._scale:  # e.g. the window moved to another display
                self._settle()
                return
        scale = self._scale
        if not scale:
            return

        wanted: dict[tuple, tuple[int, int, QRectF | None]] = {}
        reach = view_rect.height() * 0.6
        near = view_rect.adjusted(0, -reach, 0, reach)
        pages = self._pages_in(near)
        for index in pages:
            item = self._items[index]
            visible = item.mapRectFromScene(view_rect) & item.rect
            for col, row in item.cells(item.mapRectFromScene(near) & item.rect, scale):
                key = (self.page_revs.get(index, 0), index, scale, col, row)
                if key in self.tiles or key in self._failed:
                    continue
                clip = item.cell_rect(col, row, scale)
                wanted[key] = (1 if clip.intersects(visible) else 2, index, clip)
        for index in pages:  # links are cheap: learn them for whatever is near
            if index not in self.links and index not in self._link_requests.values():
                self._link_requests[self._session.query("links", index)] = index
        if pages:
            for index in range(max(0, pages[0] - 4), min(len(self._items), pages[-1] + 5)):
                key = ("preview", index)
                if index not in self.previews and key not in self._failed:
                    wanted[key] = (0, index, None)

        dropped = []
        for key in [k for k in self._inflight if k not in wanted]:
            req_id, _ = self._inflight.pop(key)
            self._keys.pop(req_id, None)
            dropped.append(req_id)
        for key, (priority, index, clip) in wanted.items():
            current = self._inflight.get(key)
            if current is not None:
                if current[1] <= priority:
                    continue
                self._keys.pop(current[0], None)  # now more urgent: ask again
                dropped.append(current[0])
            if clip is None:
                req_id = self._session.preview(index, PREVIEW_WIDTH, priority)
            else:
                req_id = self._session.tile(
                    index, scale, (clip.left(), clip.top(), clip.right(), clip.bottom()), priority
                )
            self._inflight[key] = (req_id, priority)
            self._keys[req_id] = key
        self._session.cancel(dropped)
        self._check_idle()

    def _track_current(self, view_rect: QRectF) -> None:
        probe = view_rect.top() + min(view_rect.height() / 2, 200.0 / max(self._zoom, 0.01))
        index = self._row_start[max(0, bisect.bisect_right(self._tops, probe) - 1)]
        if index != self._current:
            self._current = index
            self.currentPageChanged.emit(index)

    def _on_pixels(self, req_id: int, image: QImage) -> None:
        key = self._keys.pop(req_id, None)
        if key is None:
            return  # cancelled, or it belongs to another view of this tome
        self._inflight.pop(key, None)
        if key[0] == "preview":
            index = key[1]
            self.previews[index] = image
            while len(self.previews) > PREVIEW_LIMIT:
                self.previews.popitem(last=False)
            self._items[index].update()
        else:
            _, index, scale, col, row = key
            self.tiles.put(key, image)
            item = self._items[index]
            item.update(item.cell_rect(col, row, scale))
        self._check_idle()

    def _on_failed(self, req_id: int, message: str) -> None:
        key = self._keys.pop(req_id, None)
        if key is not None:
            self._inflight.pop(key, None)
            self._failed.add(key)
            self._check_idle()

    def _check_idle(self) -> None:
        if self._inflight or self._settle_timer.isActive():
            return
        if self._stale_scale is not None or self._stale_revs:
            self._stale_scale = None
            self._stale_revs.clear()
            self.viewport().update()
        self.idle.emit()

    def is_idle(self) -> bool:
        return bool(self._items) and not self._inflight and not self._settle_timer.isActive() \
            and not self._refresh_timer.isActive()

    # -- reading mode, ribbons, find highlights ----------------------------------------

    def set_reading_mode(self, mode: str) -> None:
        if mode in READING_MODES and mode != self.reading_mode:
            self.reading_mode = mode
            self.viewport().update()

    def set_ribbon_pages(self, pages) -> None:
        pages = set(pages)
        changed = pages ^ self.ribbon_pages
        self.ribbon_pages = pages
        for index in changed:
            if 0 <= index < len(self._items):
                self._items[index].update()

    def clear_hits(self) -> None:
        pages = list(self.hits)
        self.hits, self.current_hit = {}, None
        self._update_pages(pages)

    def add_hits(self, page: int, rects) -> None:
        self.hits[page] = [QRectF(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in rects]
        self._update_pages([page])

    def set_current_hit(self, page: int, index: int) -> None:
        previous = self.current_hit
        self.current_hit = (page, index)
        self._update_pages([page] + ([previous[0]] if previous else []))
        rects = self.hits.get(page)
        if rects and 0 <= index < len(rects):
            self.reveal(page, rects[index])

    def reveal(self, page: int, rect: QRectF) -> None:
        """Scroll just enough to bring `rect` (page coordinates) comfortably into view."""
        if not 0 <= page < len(self._items):
            return
        target = rect.translated(self._items[page].pos())
        view_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        if view_rect.adjusted(0, 20, 0, -20).contains(target):
            return
        self.go_to_page(page, rect.top() - view_rect.height() * 0.3)

    def _update_pages(self, pages) -> None:
        for index in set(pages):
            if 0 <= index < len(self._items):
                self._items[index].update()

    # -- tools --------------------------------------------------------------------------------

    def set_tool(self, tool: str) -> None:
        """Choose what dragging and clicking on a page does (see the *_TOOLS sets)."""
        self.commit_inline()
        self.tool = tool
        if tool in ("select", "edit_text", "add_text"):
            cursor = Qt.CursorShape.IBeamCursor
        elif tool in ("pointer", "form"):
            cursor = Qt.CursorShape.ArrowCursor
        else:
            cursor = Qt.CursorShape.CrossCursor
        self.viewport().setCursor(cursor)
        self.clear_marquee()
        self.clear_selection()
        self.preview = None
        if tool != "pointer":
            self.select_object(None)
        self.viewport().update()

    def clear_marquee(self) -> None:
        if self.marquee is not None:
            page = self.marquee[0]
            self.marquee = None
            self._update_pages([page])

    def set_object_boxes(self, page: int, boxes) -> None:
        """boxes: [(xref, (x0, y0, x1, y1), movable), ...] of things the pointer can pick."""
        self.object_boxes[page] = [(xref, QRectF(x0, y0, x1 - x0, y1 - y0), movable)
                                   for xref, (x0, y0, x1, y1), movable in boxes]
        if self.selected_object and self.selected_object[0] == page and \
                self.selected_object[1] not in {b[0] for b in self.object_boxes[page]}:
            self.select_object(None)
        self._update_pages([page])

    def set_field_boxes(self, page: int, boxes) -> None:
        self.field_boxes[page] = [QRectF(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in boxes]
        self._update_pages([page])

    def clear_field_boxes(self) -> None:
        pages = list(self.field_boxes)
        self.field_boxes = {}
        self._update_pages(pages)

    def select_object(self, target: tuple[int, int] | None) -> None:
        if target != self.selected_object:
            pages = [t[0] for t in (target, self.selected_object) if t]
            self.selected_object = target
            self._update_pages(pages)
            self.objectSelected.emit(target)

    def _link_at(self, page: int, point: QPointF) -> dict | None:
        for rect, link in self.links.get(page, ()):
            if rect.contains(point):
                return link
        return None

    def _selected_box(self) -> QRectF | None:
        if self.selected_object is None:
            return None
        page, ident = self.selected_object
        return next((box for i, box, _m in self.object_boxes.get(page, ()) if i == ident), None)

    def _handle_at(self, page: int, point: QPointF) -> str | None:
        """Which corner handle of the selected object is under `point` ('tl', 'br', ...)."""
        if not self.resizable or self.selected_object is None \
                or self.selected_object[0] != page:
            return None
        box = self._selected_box()
        if box is None:
            return None
        reach = (HANDLE + 3) / (self._zoom * PT_TO_PX)
        for name, corner in (("tl", box.topLeft()), ("tr", box.topRight()),
                             ("bl", box.bottomLeft()), ("br", box.bottomRight())):
            if abs(point.x() - corner.x()) <= reach and abs(point.y() - corner.y()) <= reach:
                return name
        return None

    @staticmethod
    def _resized(box: QRectF, handle: str, point: QPointF, free: bool) -> QRectF:
        """`box` with corner `handle` dragged to `point`; proportions kept unless `free`."""
        fixed = {"tl": box.bottomRight(), "tr": box.bottomLeft(),
                 "bl": box.topRight(), "br": box.topLeft()}[handle]
        sign_x = 1 if handle in ("tr", "br") else -1
        sign_y = 1 if handle in ("bl", "br") else -1
        width = max(8.0, (point.x() - fixed.x()) * sign_x)
        height = max(8.0, (point.y() - fixed.y()) * sign_y)
        if not free and box.width() > 0 and box.height() > 0:
            scale = max(width / box.width(), height / box.height())
            width, height = box.width() * scale, box.height() * scale
        return QRectF(fixed, QPointF(fixed.x() + width * sign_x,
                                     fixed.y() + height * sign_y)).normalized()

    def _object_at(self, page: int, point: QPointF):
        for entry in reversed(self.object_boxes.get(page, ())):
            if entry[1].adjusted(-3, -3, 3, 3).contains(point):
                return entry
        return None

    def visible_pages(self) -> range:
        """Pages at least partly on screen."""
        if not self._items:
            return range(0)
        area = self.mapToScene(self.viewport().rect()).boundingRect()
        return self._pages_in(area)

    # -- inline editor: a text field laid over the page -----------------------------------------

    def edit_inline(self, page: int, box: QRectF, text: str, callback, multiline: bool = False,
                    font_points: float | None = None) -> None:
        """Show an editor over `box`; `callback(text)` runs when the user commits."""
        self.commit_inline()
        if not 0 <= page < len(self._items):
            return
        area = self.mapFromScene(box.translated(self._items[page].pos())).boundingRect()
        area = area.adjusted(-2, -2, 2, 2)
        area.setWidth(max(area.width(), 70))
        area.setHeight(max(area.height(), 24))
        widget = QPlainTextEdit(self.viewport()) if multiline else QLineEdit(self.viewport())
        points = font_points if font_points else max(6.0, min(14.0, box.height() * 0.68))
        pixels = max(10, min(area.height() - 6, round(points * self._zoom * PT_TO_PX)))
        widget.setStyleSheet(
            "background: #fffdf5; color: #111111; border: 1px solid #6fbfe3; "
            f"border-radius: 0; padding: 0 2px; font-family: Helvetica; font-size: {pixels}px;")
        widget.setGeometry(area)
        if multiline:
            widget.setPlainText(text)
        else:
            widget.setText(text)
        widget.selectAll()
        widget.installEventFilter(self._inline_filter)
        self._inline = (widget, callback, multiline)
        widget.show()
        widget.setFocus()

    def commit_inline(self, accept: bool = True) -> None:
        if self._inline is None:
            return
        widget, callback, multiline = self._inline
        self._inline = None
        text = widget.toPlainText() if multiline else widget.text()
        widget.removeEventFilter(self._inline_filter)
        widget.hide()
        widget.deleteLater()
        if accept:
            callback(text)

    def has_inline_editor(self) -> bool:
        return self._inline is not None

    # -- text selection -------------------------------------------------------------------

    def _page_point(self, pos: QPoint, clamp: bool = False):
        """(page index, point in that page's coordinates) under a viewport position.

        With `clamp`, a position off any page is pulled onto the nearest one.
        """
        if not self._items:
            return None
        scene = self.mapToScene(pos)
        last = max(0, bisect.bisect_right(self._tops, scene.y()) - 1)
        row = range(self._row_start[last], self._row_end[last] + 1)
        for index in row:
            item = self._items[index]
            local = scene - item.pos()
            if item.rect.contains(local):
                return index, local
        if not clamp:
            return None
        bottom = max(self._items[i].pos().y() + self._items[i].rect.height() for i in row)
        if scene.y() > bottom + GAP / 2 and row[-1] + 1 < len(self._items):
            nxt = row[-1] + 1  # past the gap: belongs to the next row
            row = range(self._row_start[nxt], self._row_end[nxt] + 1)
        index = min(row, key=lambda i: abs(
            scene.x() - (self._items[i].pos().x() + self._items[i].rect.width() / 2)))
        return index, self._clamped(index, pos)

    def _clamped(self, page: int, pos: QPoint) -> QPointF:
        """A viewport position as a point on `page`, pulled inside the page's edges."""
        item = self._items[page]
        local = self.mapToScene(pos) - item.pos()
        return QPointF(min(max(local.x(), 0.0), item.rect.width()),
                       min(max(local.y(), 0.0), item.rect.height()))

    def _need_words(self, page: int) -> None:
        if page in self._words or page in self._word_requests.values():
            return
        if self._session is not None:
            self._word_requests[self._session.words(page)] = page

    def _on_reply(self, req_id: int, payload) -> None:
        if req_id in self._link_requests:
            page = self._link_requests.pop(req_id)
            self.links[page] = [
                (QRectF(x0, y0, x1 - x0, y1 - y0), link)
                for link in payload for x0, y0, x1, y1 in (link["rect"],)]
            return
        page = self._word_requests.pop(req_id, None)
        if page is None:
            return
        self._words[page] = payload
        while len(self._words) > WORD_CACHE:
            self._words.popitem(last=False)
        if self._anchor is not None and self._focus is not None and self.tool in TEXT_TOOLS:
            self._update_selection()
            if self._press_pos is None and self._selection_ready():
                self._finish_text_drag()  # the drag ended before the words arrived

    def _selection_ready(self) -> bool:
        """Are the word boxes known for every page the current drag spans?"""
        if self._anchor is None or self._focus is None:
            return True
        low, high = sorted((self._anchor[0], self._focus[0]))
        return all(page in self._words for page in range(low, high + 1))

    def _finish_text_drag(self) -> None:
        """A drag over text has ended and its words are known: select or mark them."""
        tool, self._pending_markup = self._pending_markup, None
        if tool is None:
            self.selectionChanged.emit(self.has_selection())
            return
        boxes = self.selection_boxes()
        self.clear_selection()
        if boxes:
            self.markupDrawn.emit(tool, boxes)

    def words(self, page: int) -> list | None:
        """Word boxes of a page if already known (asks the host otherwise)."""
        words = self._words.get(page)
        if words is None:
            self._need_words(page)
        return words

    def _update_selection(self, word_only: bool = False) -> None:
        chosen: dict[int, list] = {}
        if self._anchor is not None and self._focus is not None:
            first, last = sorted((self._anchor, self._focus), key=lambda hit: hit[0])
            for page in range(first[0], last[0] + 1):
                words = self._words.get(page)
                if words is None:
                    self._need_words(page)
                    continue
                if not words:
                    continue
                if word_only:
                    index = selection.word_at(words, self._anchor[1].x(), self._anchor[1].y())
                    if index is not None:
                        chosen[page] = [words[index]]
                    continue
                low = selection.nearest_word(words, first[1].x(), first[1].y()) \
                    if page == first[0] else 0
                high = selection.nearest_word(words, last[1].x(), last[1].y()) \
                    if page == last[0] else len(words) - 1
                if low > high:
                    low, high = high, low
                chosen[page] = words[low:high + 1]
        touched = set(self._selected) | set(chosen)
        self._selected = chosen
        self.selection_rects = {
            page: [QRectF(x0, y0, x1 - x0, y1 - y0)
                   for x0, y0, x1, y1 in selection.line_rects(words)]
            for page, words in chosen.items()
        }
        self._update_pages(touched)

    def has_selection(self) -> bool:
        return any(self._selected.values())

    def selected_text(self) -> str:
        return "\n".join(selection.text_of(self._selected[page])
                         for page in sorted(self._selected) if self._selected[page])

    def selection_boxes(self) -> dict[int, list[tuple]]:
        """{page: [(x0, y0, x1, y1), ...]}: one box per selected line."""
        return {page: [(r.left(), r.top(), r.right(), r.bottom()) for r in rects]
                for page, rects in self.selection_rects.items() if rects}

    def clear_selection(self) -> None:
        had = self.has_selection()
        self._anchor = self._focus = None
        self._update_selection()
        if had:
            self.selectionChanged.emit(False)

    def select_all_on_page(self, page: int | None = None) -> None:
        page = self.current_page() if page is None else page
        if not 0 <= page < len(self._items):
            return
        rect = self._items[page].rect
        self._anchor = (page, QPointF(0, 0))
        self._focus = (page, QPointF(rect.width(), rect.height()))
        self._need_words(page)
        self._update_selection()
        self.selectionChanged.emit(self.has_selection())

    # -- mouse: what a press, drag and release mean depends on the tool ---------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.commit_inline()
        self.setFocus()
        pos = event.position().toPoint()
        self.clear_selection()
        self.clear_marquee()
        hit = self._page_point(pos)
        self._press_pos = pos if hit is not None else None
        self._selecting = False
        self._pending_markup = None
        self._anchor, self._focus = hit, None
        self.preview = self._grab = None
        self._pressed_link = None
        tool = self.tool
        if hit is None:
            if tool == "pointer":
                self.select_object(None)
        elif tool in TEXT_TOOLS:
            if tool == "select":
                self._pressed_link = self._link_at(*hit)
            self._need_words(hit[0])
        elif tool == "ink":
            self.preview = ("ink", hit[0], [hit[1]])
        elif tool == "pointer":
            handle = self._handle_at(*hit)
            if handle is not None:  # resize the object that is already selected
                self._grab = (hit[0], self.selected_object[1], QRectF(self._selected_box()),
                              hit[1], handle)
            else:
                found = self._object_at(*hit)
                self.select_object((hit[0], found[0]) if found else None)
                if found and found[2]:
                    self._grab = (hit[0], found[0], QRectF(found[1]), hit[1], None)
        event.accept()

    def _hover(self, pos: QPoint) -> None:
        """Show what a click here would do: follow a link, or resize the selection."""
        hit = self._page_point(pos)
        shape = None
        if hit is not None:
            if self.tool == "select" and self._link_at(*hit) is not None:
                shape = Qt.CursorShape.PointingHandCursor
            elif self.tool == "pointer":
                handle = self._handle_at(*hit)
                if handle in ("tl", "br"):
                    shape = Qt.CursorShape.SizeFDiagCursor
                elif handle in ("tr", "bl"):
                    shape = Qt.CursorShape.SizeBDiagCursor
        if shape is None:
            shape = Qt.CursorShape.IBeamCursor if self.tool in ("select", "edit_text",
                                                                "add_text") \
                else Qt.CursorShape.ArrowCursor if self.tool in ("pointer", "form") \
                else Qt.CursorShape.CrossCursor
        if self.viewport().cursor().shape() != shape:
            self.viewport().setCursor(shape)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is None or not event.buttons() & Qt.MouseButton.LeftButton:
            self._hover(event.position().toPoint())
            return super().mouseMoveEvent(event)
        pos = event.position().toPoint()
        if not self._selecting and (pos - self._press_pos).manhattanLength() < 4:
            return
        self._selecting = True
        tool = self.tool
        page, start = self._anchor
        if tool in TEXT_TOOLS:
            self._focus = self._page_point(pos, clamp=True)
            self._update_selection()
        elif tool in BOX_TOOLS:
            self.marquee = (page, QRectF(start, self._clamped(page, pos)).normalized())
            self._update_pages([page])
        elif tool in LINE_TOOLS:
            self.preview = ("line", page, start, self._clamped(page, pos))
            self._update_pages([page])
        elif tool == "ink" and self.preview is not None:
            self.preview[2].append(self._clamped(page, pos))
            self._update_pages([page])
        elif tool == "pointer" and self._grab is not None:
            point = self._clamped(page, pos)
            if self._grab[4] is not None:  # dragging a corner: resize
                free = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                box = self._resized(self._grab[2], self._grab[4], point, free)
            else:
                box = self._grab[2].translated(point - self._grab[3])
            self.preview = ("box", page, box)
            self._update_pages([page])
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return super().mouseReleaseEvent(event)
        self._press_pos = None
        tool = self.tool
        page, start = self._anchor
        where = event.globalPosition().toPoint()
        preview, grab = self.preview, self._grab
        self.preview = self._grab = None
        if not self._selecting:  # a plain click
            if tool in CLICK_TOOLS:
                self.pageClicked.emit(tool, page, start, where)
            elif tool == "select" and self._pressed_link is not None:
                self.linkActivated.emit(self._pressed_link)
        else:
            self._selecting = False
            if tool in TEXT_TOOLS:
                self._pending_markup = None if tool == "select" else tool
                if self._selection_ready():
                    self._finish_text_drag()
                # otherwise _on_reply finishes the job when the word boxes arrive
            elif tool in BOX_TOOLS:
                box = self.marquee[1] if self.marquee is not None else QRectF()
                if box.width() > 4 and box.height() > 4:
                    if tool == "excerpt":
                        self.excerptDrawn.emit(page, box, where)
                    else:
                        self.boxDrawn.emit(tool, page, box, where)
                else:
                    self.clear_marquee()
            elif tool in LINE_TOOLS and preview is not None:
                a, b = preview[2], preview[3]
                if abs(a.x() - b.x()) + abs(a.y() - b.y()) > 4:
                    self.lineDrawn.emit(tool, page, a, b)
            elif tool == "ink" and preview is not None and len(preview[2]) >= 2:
                self.inkDrawn.emit(page, [(p.x(), p.y()) for p in preview[2]])
            elif tool == "pointer" and grab is not None and preview is not None:
                self.objectMoved.emit(page, grab[1], preview[2])
        self._update_pages([page])
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        hit = self._page_point(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton or hit is None:
            return super().mouseDoubleClickEvent(event)
        if self.tool == "select":
            self._anchor = self._focus = hit
            self._need_words(hit[0])
            self._update_selection(word_only=True)
            self.selectionChanged.emit(self.has_selection())
        elif self.tool == "pointer":
            found = self._object_at(*hit)
            if found:
                self.objectActivated.emit(hit[0], found[0])
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_object:
            self.objectDeleteRequested.emit(*self.selected_object)
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.clear_selection()
            self.clear_marquee()
            self.select_object(None)
            event.accept()
        else:
            super().keyPressEvent(event)

    # -- events -------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._items and self._mode in ("fit_width", "fit_page"):
            page, offset = self.current_page(), self.page_offset()
            self.set_zoom(self._fit_factor(self._mode), self._mode)
            self.go_to_page(page, offset if self._mode == "fit_width" else 0.0)
        self._schedule_refresh()

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.set_zoom(self._zoom * (1.0 + event.value()), "custom",
                              event.position().toPoint())
                return True
        return super().viewportEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self.set_zoom(self._zoom * (1.0015 ** delta), "custom",
                              event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)


class _InlineFilter(QObject):
    """Keys that end an inline edit: Return commits, Escape cancels, Tab moves on."""

    def __init__(self, view: PageView):
        super().__init__(view)
        self._view = view

    def eventFilter(self, widget, event) -> bool:
        view = self._view
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            key = event.key()
            multiline = isinstance(widget, QPlainTextEdit)
            command = event.modifiers() & Qt.KeyboardModifier.ControlModifier
            if key == Qt.Key.Key_Escape:
                view.commit_inline(False)
                view.setFocus()
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                view.commit_inline()
                view.inlineTabbed.emit(key == Qt.Key.Key_Backtab)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (not multiline or command):
                view.commit_inline()
                view.setFocus()
                return True
        elif kind == QEvent.Type.FocusOut:
            # Defer: committing destroys the widget that is still handling this event.
            QTimer.singleShot(0, lambda: view._inline and view._inline[0] is widget
                              and view.commit_inline())
        return False
