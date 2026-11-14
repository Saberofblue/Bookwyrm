"""Scriptorium dialogs: arranging pages, editing the contents list and the tome's properties."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from bookwyrm.ui.keys import MULTI_SELECT
from bookwyrm.ui.library import dialogs as library_dialogs
from bookwyrm.ui.reader.side_panels import PAGE_ROLE, ThumbnailPanel


class _OrganizerList(ThumbnailPanel):
    """Page thumbnails that can be multi-selected and dragged into a new order."""

    reordered = Signal(list)  # current page numbers in their new order

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMovement(QListView.Movement.Snap)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSpacing(10)
        try:  # a click selects here; it does not jump the reader
            self.itemClicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.itemDoubleClicked.connect(lambda item: self.pageRequested.emit(self.row(item)))

    def order(self) -> list[int]:
        return [self.item(row).data(PAGE_ROLE) for row in range(self.count())]

    def selected_pages(self) -> list[int]:
        return sorted(self.row(item) for item in self.selectedItems())

    def dropEvent(self, event) -> None:
        super().dropEvent(event)
        order = self.order()
        if order != sorted(order):
            self.reordered.emit(order)


class PageOrganizer(QDialog):
    """Rearrange, rotate, delete, insert and extract pages. Every action is one undo step."""

    extractRequested = Signal(list)  # current page numbers to copy into a new tome

    def __init__(self, pane, title: str):
        super().__init__(pane)
        self.pane = pane
        self.session = pane.session
        self.tools = pane.scriptorium
        self.setWindowTitle(f"Pages — {title}")
        self.resize(940, 660)

        heading = QLabel("Arrange pages")
        heading.setObjectName("heading")
        hint = QLabel(f"Drag pages to reorder them. Select several with {MULTI_SELECT}. "
                      "Nothing is written to the file until you save the tome.")
        hint.setObjectName("subheading")
        hint.setWordWrap(True)
        self.list = _OrganizerList(self)
        self.list.setIconSize(QSize(150, 200))

        def button(text: str, tip: str, slot) -> QPushButton:
            made = QPushButton(text)
            made.setToolTip(tip)
            made.clicked.connect(lambda _checked=False: slot())
            return made

        self._needs_selection = [
            button("⟲ Rotate Left", "Turn the selected pages a quarter turn left",
                   lambda: self._rotate(-90)),
            button("⟳ Rotate Right", "Turn the selected pages a quarter turn right",
                   lambda: self._rotate(90)),
            button("Delete", "Remove the selected pages from the tome", self._delete),
            button("Extract to New Tome…", "Copy the selected pages into a new tome",
                   self._extract),
        ]
        inserts = [
            button("Insert Blank Page", "Add an empty page after the selection (or at the end)",
                   self._insert_blank),
            button("Insert from PDF…", "Add the pages of another PDF after the selection",
                   self._insert_pdf),
        ]
        self.undo = button("Undo", "Undo the last change", self.tools.undo)
        self.redo = button("Redo", "Redo", self.tools.redo)
        done = button("Done", "Close this window", self.close)
        done.setDefault(True)

        row_one = QHBoxLayout()
        for made in self._needs_selection[:3] + inserts + self._needs_selection[3:]:
            row_one.addWidget(made)
        row_one.addStretch(1)
        row_two = QHBoxLayout()
        row_two.addWidget(self.undo)
        row_two.addWidget(self.redo)
        row_two.addStretch(1)
        row_two.addWidget(done)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(hint)
        layout.addLayout(row_one)
        layout.addWidget(self.list, 1)
        layout.addLayout(row_two)

        self.list.reordered.connect(
            lambda order: self.tools.edit("reorder_pages", order=order))
        self.list.pageRequested.connect(pane.view.go_to_page)
        self.list.itemSelectionChanged.connect(self._sync)
        self.session.edited.connect(self._on_edited)
        self.session.saved.connect(lambda *_: self._sync())
        self._reload()

    def _reload(self) -> None:
        """Rebuild the thumbnails; a selection survives changes that keep the page count."""
        chosen, before = self.list.selected_pages(), self.list.count()
        info = self.session.info or {}
        self.list.set_session(self.session, info.get("sizes", []))
        if self.list.count() == before:  # e.g. a rotation: the same pages are still there
            for row in chosen:
                self.list.item(row).setSelected(True)
        self._sync()

    def _on_edited(self, _request, state: dict, _result) -> None:
        if state.get("structural"):
            self._reload()
        else:
            self._sync()

    def _sync(self) -> None:
        chosen = bool(self.list.selectedItems())
        for made in self._needs_selection:
            made.setEnabled(chosen)
        state = self.session.edit_state
        self.undo.setEnabled(bool(state.get("can_undo")))
        self.redo.setEnabled(bool(state.get("can_redo")))

    def _insert_point(self) -> int:
        pages = self.list.selected_pages()
        return pages[-1] + 1 if pages else self.list.count()

    def _rotate(self, angle: int) -> None:
        self.tools.edit("rotate_pages", pages=self.list.selected_pages(), angle=angle)

    def _delete(self) -> None:
        pages = self.list.selected_pages()
        if len(pages) >= self.list.count():
            library_dialogs.complain(self, "A tome needs at least one page.")
            return
        self.tools.edit("delete_pages", pages=pages)

    def _insert_blank(self) -> None:
        self.tools.edit("insert_blank", at=self._insert_point())

    def _insert_pdf(self) -> None:
        settings = QSettings()
        start = settings.value("import_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Insert pages from", start,
                                              "PDF files (*.pdf)")
        if path:
            self.tools.edit("insert_pdf", path=path, at=self._insert_point())

    def _extract(self) -> None:
        self.extractRequested.emit(self.list.selected_pages())

    def closeEvent(self, event) -> None:
        try:
            self.session.edited.disconnect(self._on_edited)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)


def extract_destination(title: str, pages: list[int]) -> str:
    """A temporary file name that reads well once imported as a tome title."""
    if len(pages) == 1:
        span = f"page {pages[0] + 1}"
    elif pages == list(range(pages[0], pages[-1] + 1)):
        span = f"pages {pages[0] + 1}-{pages[-1] + 1}"
    else:
        span = f"{len(pages)} pages"
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title).strip(" .")[:90].rstrip(" .") or "Tome"
    return str(Path(tempfile.mkdtemp(prefix="bookwyrm-extract-")) / f"{safe} ({span}).pdf")


class ContentsEditor(QDialog):
    """Edit the tome's table of contents as a simple list of level, title and page."""

    def __init__(self, toc: list, page_count: int, current_page: int, parent=None):
        super().__init__(parent)
        self.page_count = page_count
        self.current_page = current_page
        self.setWindowTitle("Contents")
        self.resize(640, 620)

        heading = QLabel("Edit contents")
        heading.setObjectName("heading")
        hint = QLabel("Level 1 is a top-level entry; level 2 sits beneath the level 1 above it, "
                      "and so on. Double-click a cell to change it.")
        hint.setObjectName("subheading")
        hint.setWordWrap(True)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Level", "Title", "Page"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for level, title, page, *_rest in toc:
            self._append(level, title, page)

        def button(text: str, tip: str, slot) -> QPushButton:
            made = QPushButton(text)
            made.setToolTip(tip)
            made.clicked.connect(lambda _checked=False: slot())
            return made

        tools = QHBoxLayout()
        for made in (
            button("Add Entry", "Add an entry for the page you are reading", self._add),
            button("Remove", "Remove the selected entry", self._remove),
            button("↑", "Move the entry up", lambda: self._move(-1)),
            button("↓", "Move the entry down", lambda: self._move(1)),
            button("← Outdent", "Make the entry one level shallower", lambda: self._shift(-1)),
            button("Indent →", "Make the entry one level deeper", lambda: self._shift(1)),
        ):
            tools.addWidget(made)
        tools.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        layout.addLayout(tools)
        layout.addWidget(buttons)

    def _append(self, level, title, page, row: int | None = None) -> int:
        row = self.table.rowCount() if row is None else row
        self.table.insertRow(row)
        for column, value in enumerate((level, title, page)):
            item = QTableWidgetItem(str(value))
            if column != 1:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, column, item)
        return row

    def _row(self) -> int:
        return self.table.currentRow()

    def _add(self) -> None:
        row = self._row()
        level = int(self.table.item(row, 0).text() or 1) if row >= 0 else 1
        new = self._append(level, "New entry", self.current_page + 1,
                           row + 1 if row >= 0 else None)
        self.table.setCurrentCell(new, 1)
        self.table.editItem(self.table.item(new, 1))

    def _remove(self) -> None:
        if self._row() >= 0:
            self.table.removeRow(self._row())

    def _move(self, step: int) -> None:
        row = self._row()
        target = row + step
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        values = [self.table.item(row, c).text() for c in range(3)]
        self.table.removeRow(row)
        self._append(*values, row=target)
        self.table.setCurrentCell(target, 1)

    def _shift(self, step: int) -> None:
        row = self._row()
        if row < 0:
            return
        item = self.table.item(row, 0)
        try:
            level = int(item.text())
        except ValueError:
            level = 1
        item.setText(str(max(1, level + step)))

    def toc(self) -> list[list]:
        """The edited list as [[level, title, page], ...]. Raises ValueError if unusable."""
        entries, previous = [], 0
        for row in range(self.table.rowCount()):
            title = self.table.item(row, 1).text().strip()
            try:
                level = int(self.table.item(row, 0).text())
                page = int(self.table.item(row, 2).text())
            except ValueError:
                raise ValueError(f"Row {row + 1}: level and page must be whole numbers.") \
                    from None
            if not title:
                raise ValueError(f"Row {row + 1} needs a title.")
            if level < 1 or level > previous + 1:
                raise ValueError(
                    f"“{title}” is indented too far. An entry can be at most one level "
                    "deeper than the entry above it.")
            if not 1 <= page <= self.page_count:
                raise ValueError(f"“{title}” points to page {page}; this tome has pages "
                                 f"1 to {self.page_count}.")
            entries.append([level, title, page])
            previous = level
        return entries

    def _accept(self) -> None:
        try:
            self.toc()
        except ValueError as problem:
            library_dialogs.complain(self, str(problem))
            return
        self.accept()


class PropertiesDialog(QDialog):
    """Edit the title, author, subject and keywords stored inside the PDF."""

    def __init__(self, metadata: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Properties")
        self.setMinimumWidth(520)
        heading = QLabel("Tome properties")
        heading.setObjectName("heading")
        self.fields = {}
        form = QFormLayout()
        form.setSpacing(10)
        for key, label in (("title", "Title"), ("author", "Author"), ("subject", "Subject"),
                           ("keywords", "Keywords")):
            field = QLineEdit(str(metadata.get(key) or ""))
            self.fields[key] = field
            form.addRow(label, field)
        self.rename = QCheckBox("Also use this title and author in the library")
        self.rename.setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(heading)
        layout.addLayout(form)
        layout.addWidget(self.rename)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {key: field.text().strip() for key, field in self.fields.items()}
