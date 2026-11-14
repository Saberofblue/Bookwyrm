"""The painted bookcase: shelves of tomes you can open, select, drag and drop.

A bookcase is drawn as a stack of *sections* (one per shelf). Each section has a
carved rail carrying the shelf's brass nameplate, then as many boards as its
tomes need. A *set* is a run of tomes between two pewter bookends with its own
small plaque above.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFontMetrics, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QToolTip

from bookwyrm.db.repo import Book
from bookwyrm.ui.theme import icons, painters, palette, theme

MIME_BOOKS = "application/x-bookwyrm-books"

COVER_H = 172
GAP = 18
POST_W = 20
PAD_X = 30
RAIL_H = 38
BAY_TOP = 26  # clear space above the covers; set plaques hang here
PLANK_H = 28
BOOKEND_W = 16
ROW_H = BAY_TOP + COVER_H + PLANK_H  # at the default cover height
MIN_COVER_H, MAX_COVER_H = 110, 260
DEFAULT_ASPECT = 0.76


@dataclass
class Group:
    """Tomes that stand together: loose on the shelf (title None) or in a named set."""

    place_id: int | None
    title: str | None
    books: list[Book]


@dataclass
class Section:
    place_id: int | None  # where a tome dropped on this section goes; None = not a drop target
    title: str
    groups: list[Group] = field(default_factory=list)
    hint: str = "This shelf awaits its first tome."


@dataclass
class _Slot:
    rect: QRect
    book: Book
    place_id: int | None

    @property
    def key(self) -> tuple:
        return (self.place_id, self.book.id)


class BookcaseView(QAbstractScrollArea):
    openRequested = Signal(int)  # book id
    contextRequested = Signal(object, object, QPoint)  # slot key | None, place id | None, pos
    booksDropped = Signal(list, int, bool)  # [(book id, from place id)], to place id, copy?
    filesDropped = Signal(list, object)  # paths, place id | None
    removeRequested = Signal(list)  # [(place id, book id)]
    selectionChanged = Signal()

    def __init__(self, cover_path, parent=None):
        super().__init__(parent)
        self._cover_path = cover_path  # book id -> Path
        self.cover_h = COVER_H  # adjustable: see set_cover_height()
        self._sections: list[Section] = []
        self._empty_text = "The shelves stand empty."
        self._slots: list[_Slot] = []
        self._rails: list[tuple[QRect, Section, int]] = []
        self._planks: list[QRect] = []
        self._bookends: list[tuple[QRect, bool]] = []
        self._plaques: list[tuple[QRect, str]] = []
        self._hints: list[tuple[QRect, str]] = []
        self._zones: list[tuple[QRect, int]] = []  # set zones first, then whole sections
        self._content_h = 0
        self._covers: dict[int, QPixmap | None] = {}
        self._aspects: dict[int, float] = {}
        self._selected: set[tuple] = set()
        self._anchor: tuple | None = None
        self._hover: tuple | None = None
        self._press_pos: QPoint | None = None
        self._drop_zone: QRect | None = None

        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().setSingleStep(40)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport().update())

    @property
    def row_h(self) -> int:
        """Height of one board: the clear space, the tomes, and the plank they stand on."""
        return BAY_TOP + self.cover_h + PLANK_H

    @property
    def _bookend_h(self) -> int:
        return min(74, round(self.cover_h * 0.43))

    def _bookend_rect(self, x: int, row_y: int) -> QRect:
        tall = self._bookend_h
        return QRect(x, row_y + BAY_TOP + self.cover_h - tall, BOOKEND_W, tall)

    def set_cover_height(self, height: int) -> None:
        """Make the tomes on the shelves bigger or smaller."""
        height = max(MIN_COVER_H, min(MAX_COVER_H, int(height)))
        if height != self.cover_h:
            self.cover_h = height
            self._covers.clear()  # cached pictures were scaled for the old size
            self._relayout()

    # -- content ----------------------------------------------------------------

    def set_sections(self, sections: list[Section], empty_text: str | None = None) -> None:
        self._sections = sections
        if empty_text is not None:
            self._empty_text = empty_text
        present = {(g.place_id, b.id) for s in sections for g in s.groups for b in g.books}
        self._selected &= present
        self._relayout()
        self.selectionChanged.emit()

    def invalidate_cover(self, book_id: int) -> None:
        self._covers.pop(book_id, None)
        self._aspects.pop(book_id, None)
        self._relayout()

    def selected(self) -> list[tuple]:
        """[(place id, book id)] in shelf order."""
        return [slot.key for slot in self._slots if slot.key in self._selected]

    def selected_books(self) -> list[Book]:
        return [slot.book for slot in self._slots if slot.key in self._selected]

    def tome_count(self) -> int:
        return len(self._slots)

    # -- covers -------------------------------------------------------------------

    def _aspect(self, book: Book) -> float:
        aspect = self._aspects.get(book.id)
        if aspect is None:
            size = QImageReader(str(self._cover_path(book.id))).size()
            aspect = size.width() / size.height() if size.isValid() and size.height() else \
                DEFAULT_ASPECT
            aspect = max(0.5, min(1.5, aspect))
            self._aspects[book.id] = aspect
        return aspect

    def _cover(self, book: Book) -> QPixmap | None:
        if book.id not in self._covers:
            pixmap = QPixmap(str(self._cover_path(book.id)))
            if pixmap.isNull():
                self._covers[book.id] = None
            else:
                dpr = self.devicePixelRatioF()
                pixmap = pixmap.scaledToHeight(round(self.cover_h * dpr),
                                               Qt.TransformationMode.SmoothTransformation)
                pixmap.setDevicePixelRatio(dpr)
                self._covers[book.id] = pixmap
        return self._covers[book.id]

    # -- layout ---------------------------------------------------------------------

    def _relayout(self) -> None:
        width = self.viewport().width()
        left, right = POST_W + PAD_X, width - POST_W - PAD_X
        self._slots, self._rails, self._planks = [], [], []
        self._bookends, self._plaques, self._hints = [], [], []
        set_zones: list[tuple[QRect, int]] = []
        section_zones: list[tuple[QRect, int]] = []
        y = 0
        for section in self._sections:
            rail = QRect(POST_W, y, width - 2 * POST_W, RAIL_H)
            count = sum(len(g.books) for g in section.groups)
            self._rails.append((rail, section, count))
            y += RAIL_H
            x, row_y = left, y

            for group in section.groups:
                in_set = group.title is not None
                first_span = True
                if in_set:
                    if x + BOOKEND_W + 90 > right and x > left:
                        row_y += self.row_h
                        x = left
                    self._bookends.append((self._bookend_rect(x, row_y), True))
                span_start = x
                if in_set:
                    x += BOOKEND_W + 5

                for book in group.books:
                    height = self.cover_h - (zlib.crc32(str(book.id).encode()) % 3) * 5
                    book_w = round(height * self._aspect(book))
                    if x + book_w > right and x > left:
                        if in_set:
                            self._close_span(set_zones, group, span_start, x - GAP, row_y,
                                             first_span)
                            first_span = False
                        row_y += self.row_h
                        x = left
                        span_start = x
                    rect = QRect(x, row_y + BAY_TOP + self.cover_h - height, book_w, height)
                    self._slots.append(_Slot(rect, book, group.place_id))
                    x += book_w + GAP

                if in_set:
                    x = x - GAP + 5 if group.books else x + 54
                    self._bookends.append((self._bookend_rect(x, row_y), False))
                    x += BOOKEND_W
                    self._close_span(set_zones, group, span_start, x, row_y, first_span)
                    x += GAP + 8

            rows = (row_y - y) // self.row_h + 1
            for row in range(rows):
                self._planks.append(QRect(POST_W, y + row * self.row_h + BAY_TOP + self.cover_h,
                                          width - 2 * POST_W, PLANK_H))
            if section.place_id is not None:
                section_zones.append(
                    (QRect(POST_W, rail.top(), rail.width(), RAIL_H + rows * self.row_h),
                     section.place_id))
            if count == 0 and not any(g.title is not None for g in section.groups):
                self._hints.append(
                    (QRect(left, y + BAY_TOP, right - left, self.cover_h), section.hint))
            y += rows * self.row_h
        self._zones = set_zones + section_zones
        self._content_h = y + (36 if self._sections else 0)
        bar = self.verticalScrollBar()
        bar.setPageStep(self.viewport().height())
        bar.setRange(0, max(0, self._content_h - self.viewport().height()))
        self.viewport().update()

    def _close_span(self, set_zones, group: Group, start_x: int, end_x: int, row_y: int,
                    first: bool) -> None:
        """Record one row's worth of a set: its drop zone, and its plaque on the first row."""
        zone = QRect(start_x - 5, row_y, end_x - start_x + 10, self.row_h - PLANK_H)
        set_zones.append((zone, group.place_id))
        if first:
            wide = min(painters.plaque_width(group.title, 10), max(zone.width(), 70))
            self._plaques.append((QRect(start_x, row_y + 4, wide, 18), group.title))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    # -- painting -----------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        view = self.viewport().rect()
        scroll = self.verticalScrollBar().value()
        painters.draw_back_panel(painter, view, scroll)
        if not self._sections:
            self._paint_empty(painter, view)
            return
        painter.translate(0, -scroll)
        visible = view.translated(0, scroll)

        for index, plank in enumerate(self._planks):
            if plank.adjusted(0, 0, 0, 16).intersects(visible):
                painters.draw_plank(painter, plank, index + 1)
        for index, (rail, section, count) in enumerate(self._rails):
            if rail.intersects(visible):
                self._paint_rail(painter, rail, section, count, index)

        painter.setFont(theme.body_font(15, italic=True))
        painter.setPen(QColor(255, 235, 205, 95))
        for rect, text in self._hints:
            if rect.intersects(visible):
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        for rect, facing_right in self._bookends:
            if rect.intersects(visible):
                painters.draw_bookend(painter, rect, facing_right)
        title_font = theme.body_font(12)
        metrics = QFontMetrics(title_font)
        for slot in self._slots:
            rect = slot.rect
            if not rect.adjusted(-8, -10, 14, PLANK_H).intersects(visible):
                continue
            lifted = rect.translated(0, -6) if slot.key == self._hover else rect
            painters.draw_cover(painter, lifted, self._cover(slot.book), slot.book.title,
                                selected=slot.key in self._selected,
                                dimmed=not slot.book.ready)
            if not slot.book.ready:
                painter.setFont(theme.body_font(12, italic=True))
                painter.setPen(QColor(palette.FROST_300))
                painter.drawText(lifted, Qt.AlignmentFlag.AlignCenter, "Cataloguing…")
            label = QRect(rect.left() - 6, rect.bottom() + 1, rect.width() + GAP - 6, PLANK_H)
            painter.setFont(title_font)
            text = metrics.elidedText(slot.book.title, Qt.TextElideMode.ElideRight, label.width())
            painter.setPen(QColor(0, 0, 0, 170))
            painter.drawText(label.translated(0, 1), Qt.AlignmentFlag.AlignCenter, text)
            painter.setPen(QColor("#ecdcb9"))
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, text)
        for rect, text in self._plaques:
            if rect.intersects(visible):
                painters.draw_plaque(painter, rect, text, 10)

        if self._drop_zone is not None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            glow = QColor(palette.FROST_500)
            glow.setAlpha(40)
            painter.setBrush(glow)
            painter.setPen(QPen(QColor(palette.FROST_300), 2, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(self._drop_zone.adjusted(3, 3, -3, -3), 6, 6)

        painter.resetTransform()
        for x in (0, view.width() - POST_W):
            painters.draw_post(painter, QRect(x, 0, POST_W, view.height()))

    def _paint_rail(self, painter, rail, section, count, index) -> None:
        painters.draw_rail(painter, rail, index + 1)
        x = rail.left() + PAD_X
        if section.title:
            width = min(painters.plaque_width(section.title, 13), rail.width() - 2 * PAD_X - 90)
            painters.draw_plaque(painter, QRect(x, rail.top() + 7, width, 24), section.title, 13)
        painter.setFont(theme.body_font(13, italic=True))
        painter.setPen(QColor(255, 235, 205, 120))
        tally = "empty" if count == 0 else f"{count} tome{'s' if count != 1 else ''}"
        painter.drawText(rail.adjusted(0, 0, -PAD_X, 0),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tally)

    def _paint_empty(self, painter, view) -> None:
        size = 120
        crest = icons.pixmap("wyvern", size, "#3d4a63")
        painter.drawPixmap(view.center().x() - size // 2, view.center().y() - size - 10, crest)
        painter.setFont(theme.body_font(18, italic=True))
        painter.setPen(QColor(palette.SILVER_500))
        painter.drawText(QRect(view.left() + 40, view.center().y() + 20, view.width() - 80, 90),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                         | Qt.TextFlag.TextWordWrap, self._empty_text)

    # -- hit testing ----------------------------------------------------------------------

    def _content_pos(self, pos: QPoint) -> QPoint:
        return QPoint(pos.x(), pos.y() + self.verticalScrollBar().value())

    def _slot_at(self, pos: QPoint) -> _Slot | None:
        point = self._content_pos(pos)
        for slot in self._slots:
            if slot.rect.adjusted(-2, -8, 2, PLANK_H).contains(point):
                return slot
        return None

    def _zone_at(self, pos: QPoint) -> tuple[QRect, int] | None:
        point = self._content_pos(pos)
        for rect, place_id in self._zones:
            if rect.contains(point):
                return rect, place_id
        return None

    # -- selection ----------------------------------------------------------------------------

    def _select(self, key: tuple | None, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        keys = [slot.key for slot in self._slots]
        if key is None:
            self._selected.clear()
        elif modifiers & Qt.KeyboardModifier.ShiftModifier and self._anchor in keys:
            a, b = sorted((keys.index(self._anchor), keys.index(key)))
            self._selected = set(keys[a:b + 1])
        elif modifiers & Qt.KeyboardModifier.ControlModifier:
            self._selected ^= {key}
            self._anchor = key
        else:
            self._selected = {key}
            self._anchor = key
        self.viewport().update()
        self.selectionChanged.emit()

    def _ensure_visible(self, rect: QRect) -> None:
        bar = self.verticalScrollBar()
        top, bottom = rect.top() - BAY_TOP - RAIL_H, rect.bottom() + PLANK_H + 8
        if top < bar.value():
            bar.setValue(top)
        elif bottom > bar.value() + self.viewport().height():
            bar.setValue(bottom - self.viewport().height())

    # -- mouse ----------------------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        slot = self._slot_at(event.position().toPoint())
        self._press_pos = event.position().toPoint() if slot else None
        if slot is None:
            self._select(None)
        elif slot.key not in self._selected or event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self._select(slot.key, event.modifiers())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            slot = self._slot_at(event.position().toPoint())
            plain = not event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                             | Qt.KeyboardModifier.ShiftModifier)
            if slot is not None and plain and len(self._selected) > 1:
                self._select(slot.key)  # a plain click on a multi-selection narrows it
        self._press_pos = None

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._press_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (pos - self._press_pos).manhattanLength() >= QApplication.startDragDistance():
                self._start_drag()
                return
        slot = self._slot_at(pos)
        key = slot.key if slot else None
        if key != self._hover:
            self._hover = key
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if slot
                                      else Qt.CursorShape.ArrowCursor)
            self.viewport().update()

    def leaveEvent(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self.viewport().update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        slot = self._slot_at(event.position().toPoint())
        if slot is not None and event.button() == Qt.MouseButton.LeftButton and slot.book.ready:
            self.openRequested.emit(slot.book.id)

    def contextMenuEvent(self, event) -> None:
        slot = self._slot_at(event.pos())
        if slot is not None and slot.key not in self._selected:
            self._select(slot.key)
        zone = self._zone_at(event.pos())
        self.contextRequested.emit(slot.key if slot else None, zone[1] if zone else None,
                                   event.globalPos())

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            slot = self._slot_at(event.pos())
            if slot is None:
                QToolTip.hideText()
            else:
                book = slot.book
                lines = [f"<b>{book.title}</b>"]
                if book.author:
                    lines.append(book.author)
                lines.append(f"{book.page_count} pages")
                QToolTip.showText(event.globalPos(), "<br>".join(lines), self.viewport())
            return True
        return super().viewportEvent(event)

    # -- keyboard ---------------------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        key = event.key()
        keys = [slot.key for slot in self._slots]
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            for book in self.selected_books():
                if book.ready:
                    self.openRequested.emit(book.id)
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected:
                self.removeRequested.emit(self.selected())
        elif key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._selected = set(keys)
            self.viewport().update()
            self.selectionChanged.emit()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and keys:
            current = keys.index(self._anchor) if self._anchor in keys else -1
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                target = current + (1 if key == Qt.Key.Key_Right else -1)
            else:  # nearest tome on the row above or below
                if current < 0:
                    target = 0
                else:
                    here = self._slots[current].rect
                    step = self.row_h if key == Qt.Key.Key_Down else -self.row_h
                    want = QPoint(here.center().x(), here.bottom() + step)
                    target = min(
                        range(len(self._slots)),
                        key=lambda i: abs(self._slots[i].rect.bottom() - want.y()) * 4
                        + abs(self._slots[i].rect.center().x() - want.x()),
                    )
            target = max(0, min(len(keys) - 1, target))
            self._select(keys[target], event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._ensure_visible(self._slots[target].rect)
        else:
            super().keyPressEvent(event)

    # -- drag and drop ---------------------------------------------------------------------

    def _start_drag(self) -> None:
        chosen = [slot for slot in self._slots if slot.key in self._selected]
        self._press_pos = None
        if not chosen:
            return
        mime = QMimeData()
        payload = [{"book": slot.book.id, "from": slot.place_id} for slot in chosen]
        mime.setData(MIME_BOOKS, json.dumps(payload).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        cover = self._cover(chosen[0].book)
        if cover is not None:
            thumb = cover.scaledToHeight(round(96 * cover.devicePixelRatio()),
                                         Qt.TransformationMode.SmoothTransformation)
            thumb.setDevicePixelRatio(cover.devicePixelRatio())
            drag.setPixmap(thumb)
        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)

    @staticmethod
    def _pdf_paths(mime) -> list[str]:
        return [url.toLocalFile() for url in mime.urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")]

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasFormat(MIME_BOOKS) or self._pdf_paths(mime):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        mime = event.mimeData()
        zone = self._zone_at(event.position().toPoint())
        files = bool(self._pdf_paths(mime))
        if mime.hasFormat(MIME_BOOKS) and zone is None:
            self._set_drop_zone(None)
            event.ignore()
            return
        self._set_drop_zone(zone[0] if zone else None)
        if files:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_zone(None)

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        zone = self._zone_at(event.position().toPoint())
        self._set_drop_zone(None)
        paths = self._pdf_paths(mime)
        if paths:
            self.filesDropped.emit(paths, zone[1] if zone else None)
            event.acceptProposedAction()
        elif mime.hasFormat(MIME_BOOKS) and zone is not None:
            items = json.loads(bytes(mime.data(MIME_BOOKS)).decode("utf-8"))
            copy = event.dropAction() == Qt.DropAction.CopyAction
            self.booksDropped.emit([(i["book"], i["from"]) for i in items], zone[1], copy)
            event.acceptProposedAction()

    def _set_drop_zone(self, rect: QRect | None) -> None:
        if rect != self._drop_zone:
            self._drop_zone = rect
            self.viewport().update()
