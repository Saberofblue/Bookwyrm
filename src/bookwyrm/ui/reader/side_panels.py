"""The reader's side panel: Contents (outline), Pages (thumbnails) and Ribbons (bookmarks)."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookwyrm.ui.keys import hint
from bookwyrm.ui.theme import icons, palette

PAGE_ROLE = Qt.ItemDataRole.UserRole
THUMB_W = 132


class OutlinePanel(QTreeWidget):
    """The tome's table of contents."""

    pageRequested = Signal(int)  # 0-based

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._entries: list[tuple[int, QTreeWidgetItem]] = []  # (page, item) in order
        self._following = False
        self.itemClicked.connect(self._clicked)

    def set_toc(self, toc: list) -> None:
        """toc: [[level, title, 1-based page], ...] as PyMuPDF reports it."""
        self.clear()
        self._entries = []
        parents: dict[int, QTreeWidgetItem] = {}
        for level, title, page, *_rest in toc:
            parent = parents.get(level - 1)
            item = QTreeWidgetItem(parent if parent is not None else self, [str(title).strip()])
            item.setData(0, PAGE_ROLE, max(0, int(page) - 1))
            item.setToolTip(0, f"{str(title).strip()} — page {page}")
            parents[level] = item
            for deeper in [k for k in parents if k > level]:
                del parents[deeper]
            self._entries.append((max(0, int(page) - 1), item))
        for index in range(self.topLevelItemCount()):
            self.topLevelItem(index).setExpanded(self.topLevelItemCount() <= 12)

    def is_empty(self) -> bool:
        return not self._entries

    def follow_page(self, page: int) -> None:
        """Highlight the section the reader is currently in."""
        current = None
        for entry_page, item in self._entries:
            if entry_page <= page:
                current = item
            else:
                break
        if current is not None and current is not self.currentItem():
            self._following = True
            self.setCurrentItem(current)
            self._following = False

    def _clicked(self, item, _column) -> None:
        page = item.data(0, PAGE_ROLE)
        if page is not None:
            self.pageRequested.emit(int(page))


class ThumbnailPanel(QListWidget):
    """Every page as a small picture; pictures load as they scroll into view."""

    pageRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session = None
        self._requests: dict[int, int] = {}  # req_id -> page
        self._loaded: set[int] = set()
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(8)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setStyleSheet("QListWidget { border: none; background: transparent; }")
        self._load_timer = QTimer(self, singleShot=True, interval=60)
        self._load_timer.timeout.connect(self._load_visible)
        self.verticalScrollBar().valueChanged.connect(lambda _: self._load_timer.start(60))
        self.itemClicked.connect(lambda item: self.pageRequested.emit(self.row(item)))

    def set_session(self, session, sizes: list) -> None:
        """Show these pages. Called again whenever the tome's pages change."""
        if self._session is not session:
            self._session = session
            session.pixels.connect(self._on_pixels)
        self.clear()
        self._requests.clear()
        self._loaded.clear()
        tallest = max((h / w for w, h in sizes if w), default=1.4)
        thumb_h = round(THUMB_W * min(tallest, 1.7))
        self.setIconSize(QSize(THUMB_W, thumb_h))
        blank = QPixmap(THUMB_W, thumb_h)
        blank.fill(QColor(palette.STONE_700))
        placeholder = QIcon(blank)
        for number in range(len(sizes)):
            item = QListWidgetItem(placeholder, str(number + 1))
            item.setData(PAGE_ROLE, number)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setSizeHint(QSize(THUMB_W + 12, thumb_h + 26))
            self.addItem(item)
        self._load_timer.start(60)

    def follow_page(self, page: int) -> None:
        if 0 <= page < self.count() and page != self.currentRow():
            self.setCurrentRow(page)
            if self.isVisible():
                self.scrollToItem(self.item(page), QAbstractItemView.ScrollHint.EnsureVisible)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._load_timer.start(60)
        if self.currentRow() >= 0:
            self.scrollToItem(self.currentItem(), QAbstractItemView.ScrollHint.PositionAtCenter)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._load_timer.start(60)

    def _load_visible(self) -> None:
        if self._session is None or not self.isVisible() or not self.count():
            return
        area = self.viewport().rect()
        first = self.indexAt(QPoint(THUMB_W // 2, 14)).row()
        if first < 0:
            first = max(0, self.indexAt(area.center()).row())
        per_screen = max(4, (area.height() // max(1, self.iconSize().height() + 34) + 2)
                         * max(1, area.width() // (THUMB_W + 20)))
        for page in range(max(0, first - 2), min(self.count(), first + per_screen + 4)):
            if page not in self._loaded and page not in self._requests.values():
                self._requests[self._session.preview(page, THUMB_W * 2, 3)] = page

    def _on_pixels(self, req_id: int, image: QImage) -> None:
        page = self._requests.pop(req_id, None)
        if page is None or page >= self.count():
            return
        size = self.iconSize()
        dpr = 2.0
        canvas = QPixmap(round(size.width() * dpr), round(size.height() * dpr))
        canvas.fill(Qt.GlobalColor.transparent)
        scaled = image.scaled(canvas.size(), Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(canvas)
        painter.drawImage((canvas.width() - scaled.width()) // 2,
                          canvas.height() - scaled.height(), scaled)
        painter.end()
        canvas.setDevicePixelRatio(dpr)
        self.item(page).setIcon(QIcon(canvas))
        self._loaded.add(page)


class RibbonPanel(QWidget):
    """Ribbons: the pages you have marked in this tome."""

    pageRequested = Signal(int)
    addRequested = Signal()
    renamed = Signal(int, str)  # ribbon id, new label
    removed = Signal(int)  # ribbon id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._building = False
        self.list = QListWidget(self)
        self.list.setStyleSheet("QListWidget { border: none; background: transparent; }")
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed
                                  | QAbstractItemView.EditTrigger.SelectedClicked)
        self.list.itemClicked.connect(self._clicked)
        self.list.itemChanged.connect(self._edited)
        self.hint = QLabel("No ribbons yet. Mark a page to find it again in a heartbeat.")
        self.hint.setObjectName("subheading")
        self.hint.setWordWrap(True)
        self.hint.setContentsMargins(10, 10, 10, 0)
        self.add = QPushButton("Add Ribbon")
        self.add.setIcon(icons.icon("bookmarklet"))
        self.add.setToolTip(f"Bookmark the current page ({hint('Ctrl+D')})")
        self.add.clicked.connect(self.addRequested)
        self.remove = QPushButton("Remove")
        self.remove.setToolTip("Remove the selected ribbon")
        self.remove.clicked.connect(self._remove_current)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(8, 6, 8, 8)
        buttons.addWidget(self.add, 1)
        buttons.addWidget(self.remove)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.hint)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        self.set_ribbons([])

    def set_ribbons(self, ribbons: list) -> None:
        self._building = True
        self.list.clear()
        for ribbon in ribbons:
            item = QListWidgetItem(icons.icon("bookmarklet", "#d9736a"),
                                   self._text(ribbon.label, ribbon.page))
            item.setData(PAGE_ROLE, (ribbon.id, ribbon.page, ribbon.label))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.addItem(item)
        self._building = False
        self.hint.setVisible(not ribbons)
        self.remove.setEnabled(bool(ribbons))

    def set_available(self, available: bool) -> None:
        """Ribbons are kept in the library, so they need a library tome."""
        self.add.setEnabled(available)
        if not available:
            self.hint.setText("Ribbons are kept for tomes in your library. "
                              "Add this PDF to the library to mark its pages.")
            self.hint.setVisible(True)

    @staticmethod
    def _text(label: str, page: int) -> str:
        return f"{label}  ·  p. {page + 1}" if label else f"Page {page + 1}"

    def _clicked(self, item) -> None:
        self.pageRequested.emit(item.data(PAGE_ROLE)[1])

    def _edited(self, item) -> None:
        if self._building:
            return
        ribbon_id, page, _old = item.data(PAGE_ROLE)
        label = item.text().split("  ·  p. ")[0].strip()
        if label == f"Page {page + 1}":
            label = ""
        self.renamed.emit(ribbon_id, label)

    def _remove_current(self) -> None:
        item = self.list.currentItem() or (self.list.item(0) if self.list.count() == 1 else None)
        if item is not None:
            self.removed.emit(item.data(PAGE_ROLE)[0])


class SidePanel(QTabWidget):
    """Contents / Pages / Ribbons, shown beside the page view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidePanel")
        self.setDocumentMode(True)
        self.setUsesScrollButtons(False)
        self.setMinimumWidth(210)
        self.outline = OutlinePanel(self)
        self.pages = ThumbnailPanel(self)
        self.ribbons = RibbonPanel(self)
        self.no_outline = QLabel("This tome has no table of contents.")
        self.no_outline.setObjectName("subheading")
        self.no_outline.setWordWrap(True)
        self.no_outline.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.no_outline.setContentsMargins(12, 12, 12, 12)
        self.addTab(self.outline, "Contents")
        self.addTab(self.pages, "Pages")
        self.addTab(self.ribbons, "Ribbons")
        self.setTabToolTip(0, "The tome's table of contents")
        self.setTabToolTip(1, "Thumbnails of every page")
        self.setTabToolTip(2, "Ribbons: your bookmarks in this tome")

    def set_toc(self, toc: list) -> None:
        self.outline.set_toc(toc)
        wanted = self.no_outline if self.outline.is_empty() else self.outline
        if self.widget(0) is not wanted:
            current = self.currentIndex()
            self.removeTab(0)
            self.insertTab(0, wanted, "Contents")
            self.setTabToolTip(0, "The tome's table of contents")
            if wanted is self.no_outline and current == 0:
                current = 1  # nothing to show here: start on the page thumbnails
            self.setCurrentIndex(current)
