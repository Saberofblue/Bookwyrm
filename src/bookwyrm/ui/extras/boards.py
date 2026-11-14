"""Study Tables (pages pinned for a session) and the Commonplace Book (clipped excerpts)."""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStackedLayout,
    QWidget,
)

from bookwyrm.db.repo import Library
from bookwyrm.pdf.session_client import DocumentSession
from bookwyrm.ui.library import dialogs
from bookwyrm.ui.theme import palette

ROLE = Qt.ItemDataRole.UserRole
CARD_W, CARD_H = 210, 250
PIN_PICTURE_W = 420  # pixels across, as drawn for a pin's card
PIN_PATIENCE = 30_000  # ms to wait for a page's picture before pinning without one


def image_bytes(image: QImage, fmt: str = "PNG", quality: int = -1) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt, quality)
    return bytes(data)


def thumbnail_bytes(image: QImage, edge: int = 420) -> bytes:
    """A small JPEG for a pin card."""
    scaled = image.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    return image_bytes(scaled, "JPG", 82)


def section_title(toc, page: int) -> str:
    """The contents entry that covers a 0-based `page`: a ready-made name for a pin or
    ribbon there. `toc` is a tome's contents as [level, title, 1-based page, ...] rows."""
    title = ""
    for _level, text, number, *_rest in toc or []:
        if number - 1 <= page:
            title = str(text).strip()
        else:
            break
    return title


class PagePinner(QObject):
    """Pins pages of tomes that need not be open in the reader.

    A pin carries a small picture of its page. The reader draws that from the tome
    it has open; here a helper process of its own draws the page from the library's
    file and is closed again straight after. Because it reads the saved file, pages
    are numbered as in the saved tome: the numbering the Card Catalogue lists.
    """

    pinned = Signal(int, int, int)  # table id, book id, 0-based page

    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self.library = library
        self._jobs: list[dict] = []

    def pin(self, table_id: int, book_id: int, page: int, fallback_label: str = "") -> bool:
        """Pin a page to a Study Table once its picture is drawn. The pin is named after
        the section of the tome the page falls in, or `fallback_label` if it has none.
        Returns False if the tome or the table is not there to pin."""
        book = self.library.book(book_id)
        if book is None or not book.ready or self.library.table(table_id) is None:
            return False
        session = DocumentSession(str(self.library.book_file(book)), self)
        job = {"session": session, "table": table_id, "book": book_id, "page": page,
               "label": fallback_label, "done": False}
        self._jobs.append(job)
        session.pixels.connect(lambda _request, image: self._finish(job, image))
        session.failed.connect(lambda _request, _message: self._finish(job, None))
        session.crashed.connect(lambda _message: self._finish(job, None))
        session.password_required.connect(lambda: self._finish(job, None))
        session.preview(page, PIN_PICTURE_W, 0)
        QTimer.singleShot(PIN_PATIENCE, lambda: self._finish(job, None))
        return True

    def busy(self) -> bool:
        return bool(self._jobs)

    def _finish(self, job: dict, image) -> None:
        """Add the pin, with the page's picture if one came (a locked or damaged tome
        gives none; the pin is still worth having)."""
        if job["done"]:
            return
        job["done"] = True
        session = job["session"]
        label = section_title((session.info or {}).get("toc"), job["page"]) or job["label"]
        picture = thumbnail_bytes(image) if image is not None and not image.isNull() else None
        library = self.library
        if library.table(job["table"]) is not None and library.book(job["book"]) is not None:
            library.add_pin(job["table"], job["book"], job["page"], None, label, picture)
            self.pinned.emit(job["table"], job["book"], job["page"])
        QTimer.singleShot(0, lambda: self._release(job))  # not from inside its own signal

    def _release(self, job: dict) -> None:
        if job in self._jobs:
            self._jobs.remove(job)
            job["session"].close()
            job["session"].deleteLater()

    def forget(self, book_id: int) -> None:
        """Drop any pinning under way for a tome: its file is about to be removed."""
        for job in [job for job in self._jobs if job["book"] == book_id]:
            job["done"] = True
            self._release(job)

    def close(self) -> None:
        """Stop every helper: the library is being closed, moved or swapped."""
        for job in list(self._jobs):
            job["done"] = True
            self._release(job)


def _card(picture: QPixmap) -> QIcon:
    """Centre a picture on a card-sized canvas so every card lines up."""
    dpr = 2.0
    canvas = QPixmap(round(CARD_W * dpr), round(CARD_H * dpr))
    canvas.fill(QColor(palette.STONE_700))
    if not picture.isNull():
        scaled = picture.scaled(canvas.size() - QSize(16, 16), Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(canvas)
        painter.drawPixmap((canvas.width() - scaled.width()) // 2,
                           (canvas.height() - scaled.height()) // 2, scaled)
        painter.end()
    canvas.setDevicePixelRatio(dpr)
    return QIcon(canvas)


class _CardList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setSpacing(14)
        self.setIconSize(QSize(CARD_W, CARD_H))
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setStyleSheet("QListWidget { border: none; background: transparent; padding: 12px; }")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def add_card(self, icon: QIcon, text: str, data, tip: str = "") -> QListWidgetItem:
        item = QListWidgetItem(icon, text)
        item.setData(ROLE, data)
        item.setToolTip(tip or text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        item.setSizeHint(QSize(CARD_W + 16, CARD_H + 58))
        self.addItem(item)
        return item


class _CardPage(QWidget):
    """A card grid with a centred hint when there is nothing to show."""

    openRegion = Signal(int, int, object)  # book id, 0-based page, rect tuple | None

    def __init__(self, library: Library, hint: str, parent=None):
        super().__init__(parent)
        self.library = library
        self.cards = _CardList(self)
        self.hint = QLabel(hint, self)
        self.hint.setObjectName("subheading")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        self.hint.setContentsMargins(60, 0, 60, 0)
        self._layout = QStackedLayout(self)
        self._layout.addWidget(self.cards)
        self._layout.addWidget(self.hint)
        self.cards.itemActivated.connect(self._open)
        self.cards.itemDoubleClicked.connect(self._open)
        self.cards.customContextMenuRequested.connect(self._menu)

    def _show_cards(self, any_cards: bool) -> None:
        self._layout.setCurrentWidget(self.cards if any_cards else self.hint)

    def _source(self, book_id, page) -> str:
        book = self.library.book(book_id) if book_id is not None else None
        if book is None:
            return "Source no longer in the library"
        if page is None:
            return book.title
        return f"{book.title} · p. {self.library.printed_page(book_id, page)}"

    def _open(self, item) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _menu(self, pos) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class StudyTablePage(_CardPage):
    """One Study Table: pinned pages laid out as cards you can reorder by dragging."""

    def __init__(self, library: Library, parent=None):
        super().__init__(library, "Nothing is pinned to this table yet.\n\nOpen a tome and use "
                         "the pin button to lay a page here, ready for your session.", parent)
        self.table_id: int | None = None
        self._loading = False
        self.cards.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.cards.setMovement(QListView.Movement.Snap)
        self.cards.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.cards.model().rowsMoved.connect(lambda *_: self._save_order())

    def show_table(self, table_id: int) -> None:
        self.table_id = table_id
        self.reload()

    def reload(self) -> None:
        if self.table_id is None:
            return
        self._loading = True
        self.cards.clear()
        pins = self.library.pins(self.table_id)
        for pin in pins:
            picture = QPixmap()
            if pin.thumb:
                picture.loadFromData(pin.thumb)
            source = self._source(pin.book_id, pin.page)
            text = f"{pin.label}\n{source}" if pin.label else source
            self.cards.add_card(_card(picture), text, pin, tip=text)
        self._loading = False
        self._show_cards(bool(pins))

    def _save_order(self) -> None:
        if self._loading or self.table_id is None:
            return
        order = [self.cards.item(row).data(ROLE).id for row in range(self.cards.count())]
        self.library.reorder_pins(self.table_id, order)

    def _open(self, item) -> None:
        pin = item.data(ROLE)
        self.openRegion.emit(pin.book_id, pin.page, pin.rect)

    def _menu(self, pos) -> None:
        item = self.cards.itemAt(pos)
        if item is None:
            return
        pin = item.data(ROLE)
        menu = QMenu(self)
        menu.addAction("Open This Page", lambda: self._open(item))
        menu.addAction("Rename…", lambda: self._rename(pin))
        menu.addSeparator()
        menu.addAction("Unpin", lambda: self.library.delete_pin(pin.id))
        menu.exec(self.cards.viewport().mapToGlobal(pos))

    def _rename(self, pin) -> None:
        label = dialogs.ask_name(self, "Rename Pin", "Label for this pin:", pin.label)
        if label is not None:
            self.library.relabel_pin(pin.id, label)


class CommonplaceBook(_CardPage):
    """Every excerpt you have clipped, newest first, each linked back to its page."""

    def __init__(self, library: Library, parent=None):
        super().__init__(library, "The Commonplace Book is blank.\n\nOpen a tome, choose the "
                         "excerpt tool (the scissors) and drag a box around a stat block, map or "
                         "passage to keep it here.", parent)
        self.reload()

    def reload(self) -> None:
        self.cards.clear()
        excerpts = self.library.excerpts()
        for excerpt in excerpts:
            picture = QPixmap(str(self.library.excerpt_file(excerpt)))
            source = self._source(excerpt.book_id, excerpt.page)
            text = f"{excerpt.note}\n{source}" if excerpt.note else source
            self.cards.add_card(_card(picture), text, excerpt, tip=text)
        self._show_cards(bool(excerpts))

    def _open(self, item) -> None:
        excerpt = item.data(ROLE)
        if excerpt.book_id is not None and excerpt.page is not None:
            self.openRegion.emit(excerpt.book_id, excerpt.page, excerpt.rect)

    def _menu(self, pos) -> None:
        item = self.cards.itemAt(pos)
        if item is None:
            return
        excerpt = item.data(ROLE)
        menu = QMenu(self)
        if excerpt.book_id is not None and self.library.book(excerpt.book_id) is not None:
            menu.addAction("Open the Source Page", lambda: self._open(item))
        menu.addAction("Copy Image", lambda: QApplication.clipboard().setPixmap(
            QPixmap(str(self.library.excerpt_file(excerpt)))))
        menu.addAction("Edit Note…", lambda: self._note(excerpt))
        menu.addSeparator()
        menu.addAction("Delete Excerpt", lambda: self._delete(excerpt))
        menu.exec(self.cards.viewport().mapToGlobal(pos))

    def _note(self, excerpt) -> None:
        note = dialogs.ask_name(self, "Excerpt Note", "A note for this excerpt:", excerpt.note)
        if note is not None:
            self.library.annotate_excerpt(excerpt.id, note)

    def _delete(self, excerpt) -> None:
        if dialogs.confirm(self, "Delete Excerpt",
                           "Delete this excerpt from the Commonplace Book?\n\nThe tome it came "
                           "from is not affected.", "Delete"):
            self.library.delete_excerpt(excerpt.id)
