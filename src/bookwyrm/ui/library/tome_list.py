"""The shelves as a plain list: one row per tome, for big collections."""

from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

from bookwyrm.db.repo import Book
from bookwyrm.ui.library.bookcase_view import MIME_BOOKS, NO_TEXT_NOTE, Section

ROLE = Qt.ItemDataRole.UserRole
COLUMNS = ("Title", "Author", "Pages", "Where", "Added", "Last read", "Labels")


class _Row(QTreeWidgetItem):
    """Sorts the Pages column as numbers and everything else without regard to case."""

    def __lt__(self, other) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column == 2:
            return int(self.text(2) or 0) < int(other.text(2) or 0)
        return self.text(column).casefold() < other.text(column).casefold()


class TomeList(QTreeWidget):
    """Shows the same sections as the painted bookcase and speaks the same signals."""

    openRequested = Signal(int)  # book id
    contextRequested = Signal(object, object, QPoint)  # slot key | None, place id | None, pos
    filesDropped = Signal(list, object)  # paths, place id | None
    removeRequested = Signal(list)  # [(place id, book id)]
    selectionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._empty_text = ""
        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels(COLUMNS)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSortingEnabled(True)
        # No column chosen: rows keep the order they are given (the Sort menu's order)
        # until the user clicks a column heading.
        self.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.itemDoubleClicked.connect(self._activated)
        self.itemSelectionChanged.connect(self.selectionChanged)

    # -- content ----------------------------------------------------------------

    def set_sections(self, sections: list[Section], empty_text: str | None = None,
                     labels_by_book: dict | None = None) -> None:
        if empty_text is not None:
            self._empty_text = empty_text
        labels_by_book = labels_by_book or {}
        chosen = set(self.selected())
        column, order = self.sortColumn(), self.header().sortIndicatorOrder()
        self.setSortingEnabled(False)  # fill in the given order
        self.clear()
        for section in sections:
            for group in section.groups:
                where = " ▸ ".join(part for part in (section.title, group.title) if part)
                for book in group.books:
                    row = _Row(self, [
                        book.title, book.author, str(book.page_count), where,
                        book.added_at[:10], (book.last_opened_at or "")[:10],
                        ", ".join(labels_by_book.get(book.id, ())),
                    ])
                    row.setData(0, ROLE, (group.place_id, book))
                    row.setToolTip(0, f"{book.title}\n{NO_TEXT_NOTE}" if book.unsearchable
                                   else book.title)
                    row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight
                                         | Qt.AlignmentFlag.AlignVCenter)
                    if not book.ready:
                        row.setText(0, f"{book.title}  (cataloguing…)")
                    if (group.place_id, book.id) in chosen:
                        row.setSelected(True)
        self.header().setSortIndicator(column, order)  # -1 = leave the given order alone
        self.setSortingEnabled(True)

    def _rows(self) -> list[QTreeWidgetItem]:
        return [self.topLevelItem(i) for i in range(self.topLevelItemCount())]

    def selected(self) -> list[tuple]:
        """[(place id, book id)] of the selected rows, top to bottom."""
        return [(row.data(0, ROLE)[0], row.data(0, ROLE)[1].id)
                for row in self._rows() if row.isSelected()]

    def selected_books(self) -> list[Book]:
        return [row.data(0, ROLE)[1] for row in self._rows() if row.isSelected()]

    def tome_count(self) -> int:
        return self.topLevelItemCount()

    def invalidate_cover(self, book_id: int) -> None:
        """The list shows no covers; present so it can stand in for the bookcase."""

    # -- interaction ---------------------------------------------------------------

    def _activated(self, row, _column) -> None:
        book = row.data(0, ROLE)[1]
        if book.ready:
            self.openRequested.emit(book.id)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            for book in self.selected_books():
                if book.ready:
                    self.openRequested.emit(book.id)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected():
            self.removeRequested.emit(self.selected())
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        row = self.itemAt(event.pos())
        if row is not None and not row.isSelected():
            self.clearSelection()
            row.setSelected(True)
        if row is None:
            self.contextRequested.emit(None, None, event.globalPos())
        else:
            place_id, book = row.data(0, ROLE)
            self.contextRequested.emit((place_id, book.id), place_id, event.globalPos())

    # -- dragging tomes out (onto the sidebar), and PDF files in -----------------------

    def mimeTypes(self) -> list[str]:
        return [MIME_BOOKS, "text/uri-list"]

    def mimeData(self, rows) -> QMimeData:
        mime = QMimeData()
        payload = [{"book": row.data(0, ROLE)[1].id, "from": row.data(0, ROLE)[0]}
                   for row in rows]
        mime.setData(MIME_BOOKS, json.dumps(payload).encode("utf-8"))
        return mime

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    @staticmethod
    def _pdf_paths(mime) -> list[str]:
        return [url.toLocalFile() for url in mime.urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")]

    def dragEnterEvent(self, event) -> None:
        if self._pdf_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        paths = self._pdf_paths(event.mimeData())
        if paths:
            self.filesDropped.emit(paths, None)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
