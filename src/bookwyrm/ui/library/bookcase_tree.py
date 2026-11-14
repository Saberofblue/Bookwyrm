"""Sidebar: the built-in spots, then every bookcase with its shelves and sets."""

from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

from bookwyrm.db.repo import PARENT_KIND, Library, LibraryError
from bookwyrm.ui.keys import hint
from bookwyrm.ui.library.bookcase_view import MIME_BOOKS
from bookwyrm.ui.theme import icons, palette, theme

MIME_PLACE = "application/x-bookwyrm-place"
KEY_ROLE = Qt.ItemDataRole.UserRole

# key, label, icon, tooltip
VIRTUAL = (
    ("catalogue", "Card Catalogue", "archive-research", "Search the text of every tome"),
    ("desk", "Reading Desk", "desk", "Tomes you have opened recently"),
    ("cart", "Returns Cart", "hand-truck", "Imported tomes that are not shelved yet"),
    ("stacks", "The Stacks", "book-pile", "Every tome in the library"),
    ("commonplace", "Commonplace Book", "notebook", "Excerpts you have clipped from your tomes"),
)
KIND_ICON = {"bookcase": "bookshelf", "shelf": "stack", "set": "book-cover"}
KIND_TIP = {
    "bookcase": "Bookcase: a top-level category",
    "shelf": "Shelf: a division of a bookcase",
    "set": "Set: a run of related tomes on a shelf",
}


class BookcaseTree(QTreeWidget):
    keySelected = Signal(object)  # ("virtual", name) | ("place", id) | ("table", id)
    booksDropped = Signal(list, int, bool)  # [(book id, from place id)], to place id, copy?
    filesDropped = Signal(list, object)  # paths, place id | None
    contextRequested = Signal(object, QPoint)  # key | None, global position
    complaint = Signal(str)  # a rule was broken; show this to the user

    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self.library = library
        self._building = False
        self._collapsed: set[int] = set()
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setIconSize(QSize(18, 18))
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed
                             | QAbstractItemView.EditTrigger.SelectedClicked)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.currentItemChanged.connect(self._on_current)
        self.itemChanged.connect(self._on_edited)
        self.itemCollapsed.connect(lambda item: self._note_collapse(item, True))
        self.itemExpanded.connect(lambda item: self._note_collapse(item, False))
        self.rebuild()

    # -- building ---------------------------------------------------------------

    def rebuild(self) -> None:
        library = self.library
        keep = self.current_key()
        self._building = True
        self.clear()
        counts = library.counts()
        virtual_counts = {
            "catalogue": 0,
            "desk": len(library.recent_books()),
            "cart": len(library.unshelved_books()),
            "stacks": len(library.all_books()),
            "commonplace": len(library.excerpts()),
        }
        self._add_label("THE LIBRARY")
        for key, label, icon, tip in VIRTUAL:
            item = QTreeWidgetItem(self, [label, self._tally(virtual_counts[key])])
            item.setIcon(0, icons.icon(icon))
            if key == "catalogue":
                tip = f"{tip} ({hint('Ctrl+Shift+F')})"
            item.setToolTip(0, tip)
            item.setData(0, KEY_ROLE, ("virtual", key))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._style_count(item)
        self._add_label("BOOKCASES")

        by_parent: dict = {}
        for place in library.places():
            by_parent.setdefault(place.parent_id, []).append(place)

        def add(place, parent_item):
            item = QTreeWidgetItem(parent_item, [place.name, self._tally(counts.get(place.id, 0))])
            item.setIcon(0, icons.icon(KIND_ICON[place.kind]))
            item.setToolTip(0, KIND_TIP[place.kind])
            item.setData(0, KEY_ROLE, ("place", place.id))
            item.setData(1, KEY_ROLE, place.kind)
            flags = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                     | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled
                     | Qt.ItemFlag.ItemIsDropEnabled)
            item.setFlags(flags)
            self._style_count(item)
            for child in by_parent.get(place.id, []):
                add(child, item)
            item.setExpanded(place.id not in self._collapsed)

        for bookcase in by_parent.get(None, []):
            add(bookcase, self)

        self._add_label("STUDY TABLES")
        pins = library.pin_counts()
        for table in library.tables():
            item = QTreeWidgetItem(self, [table.name, self._tally(pins.get(table.id, 0))])
            item.setIcon(0, icons.icon("table"))
            item.setToolTip(0, "Study Table: pages pinned for a session")
            item.setData(0, KEY_ROLE, ("table", table.id))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                          | Qt.ItemFlag.ItemIsEditable)
            self._style_count(item)
        self._building = False
        if not self.select_key(keep):
            self.select_key(("virtual", "stacks"))

    def _add_label(self, text: str) -> None:
        item = QTreeWidgetItem(self, [text, ""])
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setFont(0, theme.display_font(10, spacing=2.0))
        item.setForeground(0, QBrush(QColor(palette.SILVER_700)))
        item.setSizeHint(0, QSize(0, 34))

    @staticmethod
    def _tally(count: int) -> str:
        return str(count) if count else ""

    @staticmethod
    def _style_count(item: QTreeWidgetItem) -> None:
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(1, QBrush(QColor(palette.SILVER_500)))

    def _note_collapse(self, item, collapsed: bool) -> None:
        key = item.data(0, KEY_ROLE)
        if self._building or not key or key[0] != "place":
            return
        (self._collapsed.add if collapsed else self._collapsed.discard)(key[1])

    # -- selection ----------------------------------------------------------------

    def current_key(self):
        item = self.currentItem()
        return item.data(0, KEY_ROLE) if item is not None else None

    def _find(self, key):
        if key is None:
            return None
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, KEY_ROLE) == key:
                return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def select_key(self, key) -> bool:
        item = self._find(key)
        if item is None:
            return False
        self.setCurrentItem(item)
        return True

    def edit_place(self, place_id: int) -> None:
        self.edit_key(("place", place_id))

    def edit_key(self, key) -> None:
        item = self._find(key)
        if item is not None:
            self.setCurrentItem(item)
            self.editItem(item, 0)

    def _on_current(self, item, _previous) -> None:
        if self._building or item is None:
            return
        key = item.data(0, KEY_ROLE)
        if key:
            self.keySelected.emit(key)

    def _on_edited(self, item, column: int) -> None:
        if self._building or column != 0:
            return
        key = item.data(0, KEY_ROLE)
        if not key or key[0] not in ("place", "table"):
            return
        try:
            if key[0] == "table":
                self.library.rename_table(key[1], item.text(0))
            else:
                self.library.rename_place(key[1], item.text(0))
        except LibraryError as problem:
            self.complaint.emit(str(problem))
            self.rebuild()

    def contextMenuEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        key = item.data(0, KEY_ROLE) if item is not None else None
        if item is not None and key:
            self.setCurrentItem(item)
        self.contextRequested.emit(key, event.globalPos())

    # -- drag and drop --------------------------------------------------------------

    def mimeTypes(self) -> list[str]:
        return [MIME_PLACE, MIME_BOOKS, "text/uri-list"]

    def mimeData(self, items) -> QMimeData:
        mime = QMimeData()
        for item in items:
            key = item.data(0, KEY_ROLE)
            if key and key[0] == "place":
                mime.setData(MIME_PLACE, json.dumps(key[1]).encode("utf-8"))
                break
        return mime

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    @staticmethod
    def _pdf_paths(mime) -> list[str]:
        return [url.toLocalFile() for url in mime.urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")]

    def _place_at(self, pos):
        item = self.itemAt(pos)
        key = item.data(0, KEY_ROLE) if item is not None else None
        if key and key[0] == "place":
            return item, self.library.place(key[1])
        return item, None

    def _place_move_target(self, mime, pos):
        """Where a dragged place would land: (place, new parent id, index) or None."""
        moving = self.library.place(json.loads(bytes(mime.data(MIME_PLACE)).decode("utf-8")))
        item, target = self._place_at(pos)
        if moving is None or target is None or target.id == moving.id:
            return None
        where = self.dropIndicatorPosition()
        on_item = where == QAbstractItemView.DropIndicatorPosition.OnItem
        if target.kind == PARENT_KIND.get(moving.kind) and on_item:
            return moving, target.id, None  # drop into a new parent, at the end
        if target.kind == moving.kind:
            siblings = [p.id for p in self.library.children(target.parent_id)
                        if p.id != moving.id]
            index = siblings.index(target.id)
            if where == QAbstractItemView.DropIndicatorPosition.BelowItem:
                index += 1
            return moving, target.parent_id, index
        return None

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasFormat(MIME_PLACE) or mime.hasFormat(MIME_BOOKS) or self._pdf_paths(mime):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)  # keeps the drop indicator and auto-scroll working
        mime = event.mimeData()
        pos = event.position().toPoint()
        if mime.hasFormat(MIME_PLACE):
            ok = self._place_move_target(mime, pos) is not None
        elif mime.hasFormat(MIME_BOOKS):
            ok = self._place_at(pos)[1] is not None
        else:
            ok = bool(self._pdf_paths(mime))
        if ok:
            if not mime.hasFormat(MIME_PLACE) and not mime.hasFormat(MIME_BOOKS):
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        pos = event.position().toPoint()
        _, target = self._place_at(pos)
        if mime.hasFormat(MIME_PLACE):
            plan = self._place_move_target(mime, pos)
            if plan is not None:
                moving, parent_id, index = plan
                try:
                    self.library.move_place(moving.id, parent_id, index)
                except LibraryError as problem:
                    self.complaint.emit(str(problem))
        elif mime.hasFormat(MIME_BOOKS) and target is not None:
            items = json.loads(bytes(mime.data(MIME_BOOKS)).decode("utf-8"))
            copy = event.dropAction() == Qt.DropAction.CopyAction
            self.booksDropped.emit([(i["book"], i["from"]) for i in items], target.id, copy)
        else:
            paths = self._pdf_paths(mime)
            if paths:
                self.filesDropped.emit(paths, target.id if target else None)
        # Handled here; never let QTreeWidget shuffle items on its own.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
