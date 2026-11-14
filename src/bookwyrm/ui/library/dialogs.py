"""Library dialogs: choosing where new tomes go, and editing a tome's details."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from bookwyrm.db.repo import CHILD_KIND, KIND_LABEL, Book, Library, LibraryError
from bookwyrm.ui.theme import icons

KIND_ICON = {"bookcase": "bookshelf", "shelf": "stack", "set": "book-cover"}
ROLE = Qt.ItemDataRole.UserRole


def ask_name(parent, title: str, label: str, text: str = "") -> str | None:
    value, ok = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Normal, text)
    value = value.strip()
    return value if ok and value else None


def complain(parent, message: str) -> None:
    QMessageBox.warning(parent, "Bookwyrm", message)


def confirm(parent, title: str, message: str, action: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, message, parent=parent)
    yes = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is yes


def create_place(parent, library: Library, kind: str, parent_id: int | None = None):
    """Ask for a name and create a bookcase, shelf or set. Returns the Place or None."""
    owner = library.place(parent_id) if parent_id is not None else None
    where = f" in “{owner.name}”" if owner is not None else ""
    name = ask_name(parent, f"New {KIND_LABEL[kind]}", f"Name for the new {kind}{where}:")
    if name is None:
        return None
    try:
        return library.add_place(kind, name, parent_id)
    except LibraryError as problem:
        complain(parent, str(problem))
        return None


class PlacePicker(QTreeWidget):
    """A tree of bookcases, shelves and sets with 'Returns Cart' on top."""

    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self.library = library
        self.setHeaderHidden(True)
        self.setIconSize(QSize(18, 18))
        self.setIndentation(16)
        self.setMinimumHeight(190)
        self.rebuild()

    def rebuild(self, select: int | None = None) -> None:
        if select is None:
            select = self.place_id()
        self.clear()
        cart = QTreeWidgetItem(self, ["Returns Cart — shelve them later"])
        cart.setIcon(0, icons.icon("hand-truck"))
        cart.setData(0, ROLE, None)
        by_parent: dict = {}
        for place in self.library.places():
            by_parent.setdefault(place.parent_id, []).append(place)
        chosen = cart

        def add(place, parent_item):
            nonlocal chosen
            item = QTreeWidgetItem(parent_item, [place.name])
            item.setIcon(0, icons.icon(KIND_ICON[place.kind]))
            item.setData(0, ROLE, place.id)
            if place.id == select:
                chosen = item
            for child in by_parent.get(place.id, []):
                add(child, item)
            item.setExpanded(True)

        for bookcase in by_parent.get(None, []):
            add(bookcase, self)
        self.setCurrentItem(chosen)
        self.scrollToItem(chosen)

    def place_id(self) -> int | None:
        item = self.currentItem()
        return item.data(0, ROLE) if item is not None else None


class ImportDialog(QDialog):
    """Lists the PDFs about to be added and asks where they should stand."""

    def __init__(self, library: Library, paths: list[str], place_id: int | None = None,
                 parent=None):
        super().__init__(parent)
        self.library = library
        self.setWindowTitle("Add Tomes")
        self.setMinimumSize(560, 560)

        heading = QLabel("Add tomes to the library")
        heading.setObjectName("heading")
        count = len(paths)
        which = "this file" if count == 1 else "these files"
        note = QLabel(f"Bookwyrm will keep its own copy of {which}; "
                      "the originals are left exactly as they are.")
        note.setObjectName("subheading")
        note.setWordWrap(True)

        files = QListWidget()
        files.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        files.setMaximumHeight(132)
        for path in paths:
            files.addItem(Path(path).name)

        where = QLabel("WHERE SHOULD THEY GO?")
        where.setObjectName("sectionLabel")
        self.picker = PlacePicker(library)
        self.picker.rebuild(select=place_id)
        self.picker.currentItemChanged.connect(lambda *_: self._sync_buttons())

        self._new_case = QPushButton("New Bookcase…")
        self._new_shelf = QPushButton("New Shelf…")
        self._new_set = QPushButton("New Set…")
        self._new_case.clicked.connect(lambda: self._create("bookcase"))
        self._new_shelf.clicked.connect(lambda: self._create("shelf"))
        self._new_set.clicked.connect(lambda: self._create("set"))
        makers = QHBoxLayout()
        for button in (self._new_case, self._new_shelf, self._new_set):
            makers.addWidget(button)
        makers.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        add = buttons.addButton(f"Add {count} Tome{'s' if count != 1 else ''}",
                                QDialogButtonBox.ButtonRole.AcceptRole)
        add.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addWidget(files)
        layout.addSpacing(6)
        layout.addWidget(where)
        layout.addWidget(self.picker, 1)
        layout.addLayout(makers)
        layout.addSpacing(6)
        layout.addWidget(buttons)
        self._sync_buttons()

    def place_id(self) -> int | None:
        return self.picker.place_id()

    def _selected_place(self):
        place_id = self.picker.place_id()
        return self.library.place(place_id) if place_id is not None else None

    def _sync_buttons(self) -> None:
        place = self._selected_place()
        self._new_shelf.setEnabled(place is not None)
        self._new_set.setEnabled(place is not None and place.kind in ("shelf", "set"))
        self._new_shelf.setToolTip("" if place else "Choose a bookcase first")
        self._new_set.setToolTip("" if self._new_set.isEnabled() else "Choose a shelf first")

    def _create(self, kind: str) -> None:
        parent_id = None
        if kind != "bookcase":
            chain = self.library.path(self.picker.place_id())
            wanted = "bookcase" if kind == "shelf" else "shelf"
            parent_id = next(p.id for p in chain if p.kind == wanted)
        place = create_place(self, self.library, kind, parent_id)
        if place is not None:
            self.picker.rebuild(select=place.id)
            self._sync_buttons()


def _size_text(size: int) -> str:
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    return f"{max(1, size // 1024)} KB"


class TomeDetailsDialog(QDialog):
    """Edit a tome's title, author, labels and notes."""

    def __init__(self, library: Library, book: Book, parent=None):
        super().__init__(parent)
        self.library = library
        self.book = book
        self.setWindowTitle("Tome Details")
        self.setMinimumWidth(520)

        heading = QLabel("Tome details")
        heading.setObjectName("heading")
        self.title = QLineEdit(book.title)
        self.author = QLineEdit(book.author)
        self.author.setPlaceholderText("Unknown")
        self.labels = QLineEdit(", ".join(library.labels_of(book.id)))
        self.labels.setPlaceholderText("Separate labels with commas, e.g. monsters, magic items")
        self.labels.setToolTip("Labels are tags you can give any tome")
        completer = QCompleter(library.all_labels(), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.labels.setCompleter(completer)
        self.notes = QPlainTextEdit(book.notes)
        self.notes.setMinimumHeight(110)

        shelved = [" ▸ ".join(p.name for p in library.path(place.id))
                   for place in library.placements(book.id)]
        facts = QLabel(
            f"{book.page_count} pages · {_size_text(book.file_size)} · "
            f"added {book.added_at[:10]}<br>"
            f"Shelved in: {'; '.join(shelved) if shelved else 'Returns Cart (not shelved)'}<br>"
            f"File: {library.book_file(book)}"
        )
        facts.setObjectName("subheading")
        facts.setWordWrap(True)
        facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Title", self.title)
        form.addRow("Author", self.author)
        form.addRow("Labels", self.labels)
        form.addRow("Notes", self.notes)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(heading)
        layout.addLayout(form)
        layout.addWidget(facts)
        layout.addWidget(buttons)

    def _save(self) -> None:
        try:
            self.library.update_book(self.book.id, title=self.title.text().strip(),
                                     author=self.author.text().strip(),
                                     notes=self.notes.toPlainText())
        except LibraryError as problem:
            complain(self, str(problem))
            return
        self.library.set_labels(self.book.id, self.labels.text().split(","))
        self.accept()


def child_kind(kind: str) -> str | None:
    return CHILD_KIND.get(kind)


class AboutDialog(QDialog):
    """What Bookwyrm is, and whose openly licensed work it is built with."""

    def __init__(self, version: str, parent=None):
        super().__init__(parent)
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QTextBrowser

        from bookwyrm.ui.theme.theme import ASSETS

        self.setWindowTitle("About Bookwyrm")
        self.resize(620, 640)
        crest = QLabel()
        picture = QPixmap(str(ASSETS / "icons" / "app-icon.png"))
        if not picture.isNull():
            picture = picture.scaled(192, 192, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            picture.setDevicePixelRatio(2.0)
            crest.setPixmap(picture)
        name = QLabel("Bookwyrm")
        name.setObjectName("heading")
        about = QLabel(f"Version {version}. A library, reader and scriptorium for your PDFs, "
                       "kept by a silver dragon.")
        about.setObjectName("subheading")
        about.setWordWrap(True)
        titles = QVBoxLayout()
        titles.addStretch(1)
        titles.addWidget(name)
        titles.addWidget(about)
        titles.addStretch(1)
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(crest)
        top.addLayout(titles, 1)
        credits = QTextBrowser()
        credits.setOpenExternalLinks(True)
        try:
            credits.setMarkdown((ASSETS / "CREDITS.md").read_text("utf-8"))
        except OSError:
            credits.setPlainText("The credits file is missing.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addWidget(credits, 1)
        layout.addWidget(buttons)
