"""The Library tab: bookcase tree on the left, the painted bookcase on the right."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bookwyrm.db.repo import CHILD_KIND, KIND_LABEL, Library, LibraryError
from bookwyrm.library.importer import Importer, safe_filename
from bookwyrm.ui import prefs, system
from bookwyrm.ui.extras.boards import CommonplaceBook, StudyTablePage
from bookwyrm.ui.extras.card_catalogue import CardCatalogue
from bookwyrm.ui.keys import hint
from bookwyrm.ui.library import dialogs
from bookwyrm.ui.library.arrange import SORTS, arrange, count
from bookwyrm.ui.library.bookcase_tree import BookcaseTree
from bookwyrm.ui.library.bookcase_view import (
    MAX_COVER_H,
    MIN_COVER_H,
    BookcaseView,
    Group,
    Section,
)
from bookwyrm.ui.library.tome_list import TomeList
from bookwyrm.ui.theme import icons, palette

VIRTUAL_TITLES = {
    "desk": ("Reading Desk", "Tomes you have opened recently",
             "Nothing has been read yet. Open a tome and it will wait for you here."),
    "cart": ("Returns Cart", "Imported tomes waiting to be shelved",
             "The cart is empty: every tome has a home."),
    "stacks": ("The Stacks", "Every tome in the library",
               "The library holds no tomes yet. Add some to begin."),
}


class LibraryPage(QWidget):
    openBook = Signal(int)
    openBookAt = Signal(int, int, str)  # book id, 0-based page, text to highlight
    openRegion = Signal(int, int, object)  # book id, 0-based page, rect tuple | None
    aboutToRemove = Signal(int)  # book id: close it everywhere before its file is deleted
    settingsRequested = Signal()
    message = Signal(str)  # short notices for the status bar

    def __init__(self, library: Library, importer: Importer, parent=None):
        super().__init__(parent)
        self.library = library
        self.importer = importer
        self._detached = False
        self._key = ("virtual", "stacks")

        # sidebar
        crest = QLabel()
        crest.setPixmap(icons.pixmap("wyvern", 30, palette.FROST_300))
        wordmark = QLabel("BOOKWYRM")
        wordmark.setObjectName("wordmark")
        brand = QHBoxLayout()
        brand.setContentsMargins(14, 14, 14, 6)
        brand.setSpacing(10)
        brand.addWidget(crest)
        brand.addWidget(wordmark)
        brand.addStretch(1)

        self.tree = BookcaseTree(library)
        new_case = QPushButton("New Bookcase")
        new_case.setIcon(icons.icon("bookshelf"))
        new_case.setToolTip("Add a top-level category")
        new_case.clicked.connect(lambda: self.create_place("bookcase"))
        new_table = QPushButton("New Study Table")
        new_table.setIcon(icons.icon("table"))
        new_table.setToolTip("Add a board for pinning pages you need in a session")
        new_table.clicked.connect(lambda: self.create_table())
        foot = QVBoxLayout()
        foot.setContentsMargins(12, 8, 12, 12)
        foot.setSpacing(6)
        settings = QPushButton("Settings…")
        settings.setToolTip(f"Where the library lives, and how Bookwyrm behaves ({hint('Ctrl+,')})")
        settings.clicked.connect(self.settingsRequested)
        foot.addWidget(new_case)
        foot.addWidget(new_table)
        foot.addWidget(settings)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        side.addLayout(brand)
        side.addWidget(self.tree, 1)
        side.addLayout(foot)

        # main area
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        self.subheading = QLabel()
        self.subheading.setObjectName("subheading")
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(self.heading)
        titles.addWidget(self.subheading)

        self.new_child = QPushButton()
        self.new_child.clicked.connect(self._new_child_clicked)
        self.add_button = QPushButton("Add Tomes…")
        self.add_button.setObjectName("primary")
        self.add_button.setIcon(icons.icon("open-book", palette.SILVER_100))
        self.add_button.setToolTip(f"Import PDFs into the library ({hint('Ctrl+O')})")
        self.add_button.clicked.connect(lambda: self.add_tomes())

        head = QHBoxLayout()
        head.setContentsMargins(22, 12, 18, 12)
        head.addLayout(titles, 1)
        head.addWidget(self.new_child)
        head.addWidget(self.add_button)
        header = QWidget()
        header.setObjectName("libraryHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        header.setLayout(head)

        # tools for the shelves: filter, label, sort, cover size, covers or list
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter these tomes…")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.setMaximumWidth(260)
        self.filter_box.setToolTip("Show only tomes whose title or author contains this")
        self.label_box = QComboBox()
        self.label_box.setToolTip("Show only tomes carrying this Label (a tag you give tomes)")
        self.label_box.setMinimumWidth(130)
        self.sort_box = QComboBox()
        self.sort_box.setToolTip("The order tomes stand in")
        for key, label in SORTS:
            self.sort_box.addItem(label, key)
        self.sort_box.setCurrentIndex(
            max(0, self.sort_box.findData(prefs.get("shelves/sort"))))
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(MIN_COVER_H, MAX_COVER_H)
        self.size_slider.setValue(prefs.get("shelves/cover_height"))
        self.size_slider.setFixedWidth(120)
        self.size_slider.setToolTip("Cover size")
        self.list_button = QToolButton()
        self.list_button.setText("List")
        self.list_button.setCheckable(True)
        self.list_button.setChecked(prefs.get("shelves/view") == "list")
        self.list_button.setToolTip("Show these tomes as a list instead of covers on shelves")
        tools = QHBoxLayout()
        tools.setContentsMargins(22, 6, 18, 6)
        tools.setSpacing(8)
        tools.addWidget(self.filter_box)
        tools.addWidget(self.label_box)
        tools.addWidget(self.sort_box)
        tools.addStretch(1)
        tools.addWidget(self.size_slider)
        tools.addWidget(self.list_button)
        self.tools = QWidget()
        self.tools.setObjectName("libraryHeader")
        self.tools.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.tools.setLayout(tools)

        self.view = BookcaseView(library.cover_file)
        self.view.set_cover_height(prefs.get("shelves/cover_height"))
        self.list = TomeList()
        self.shelves = QStackedWidget()  # the same tomes, as covers or as a list
        self.shelves.addWidget(self.view)
        self.shelves.addWidget(self.list)
        self.unsaved = lambda book_id: False  # the window says which tomes have unsaved edits
        self.catalogue = CardCatalogue(library)
        self.catalogue.openRequested.connect(self.openBookAt)
        self.stack = QStackedWidget()
        self.commonplace = CommonplaceBook(library)
        self.commonplace.openRegion.connect(self.openRegion)
        self.table_page = StudyTablePage(library)
        self.table_page.openRegion.connect(self.openRegion)
        self.stack.addWidget(self.shelves)
        self.stack.addWidget(self.catalogue)
        self.stack.addWidget(self.commonplace)
        self.stack.addWidget(self.table_page)
        right = QWidget()
        column = QVBoxLayout(right)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(header)
        column.addWidget(self.tools)
        column.addWidget(self.stack, 1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(sidebar)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([250, 900])
        self.splitter.setChildrenCollapsible(False)
        sidebar.setMinimumWidth(190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)

        # wiring
        self._refresh_timer = QTimer(self, singleShot=True, interval=0)
        self._refresh_timer.timeout.connect(self.refresh)
        library.subscribe(self._on_library_change)
        self.tree.keySelected.connect(self._on_key)
        self.tree.contextRequested.connect(self._tree_menu)
        self.tree.booksDropped.connect(self._books_dropped)
        self.tree.filesDropped.connect(self._files_dropped)
        self.tree.complaint.connect(lambda text: dialogs.complain(self, text))
        self.view.openRequested.connect(self.openBook)
        self.view.contextRequested.connect(self._view_menu)
        self.view.booksDropped.connect(self._books_dropped)
        self.view.filesDropped.connect(self._files_dropped)
        self.view.removeRequested.connect(self._take_off_shelf)
        self.view.collapseRequested.connect(
            lambda place_id, minimize: self.library.set_collapsed([place_id], minimize))
        self.list.openRequested.connect(self.openBook)
        self.list.contextRequested.connect(self._view_menu)
        self.list.filesDropped.connect(self._files_dropped)
        self.list.removeRequested.connect(self._take_off_shelf)
        self.filter_box.textChanged.connect(lambda _text: self._show())
        self.label_box.activated.connect(lambda _index: self._show())
        self.sort_box.activated.connect(self._sort_chosen)
        self.size_slider.valueChanged.connect(self._size_chosen)
        self.list_button.toggled.connect(self._view_chosen)
        importer.bookReady.connect(self.view.invalidate_cover)
        importer.batchFinished.connect(self._batch_finished)
        self.refresh()

    # -- what is shown ------------------------------------------------------------

    def current_place(self):
        if self._key[0] == "place":
            return self.library.place(self._key[1])
        return None

    def show_key(self, key) -> None:
        self.tree.select_key(key)

    def show_catalogue(self, query: str | None = None) -> None:
        self.tree.select_key(("virtual", "catalogue"))
        if query is not None:
            self.catalogue.field.setText(query)
            self.catalogue.search()
        self.catalogue.focus_field()

    def detach(self) -> None:
        """Stop following the library: it is about to be closed, moved or swapped."""
        self._detached = True
        self._refresh_timer.stop()
        self.library.unsubscribe(self._on_library_change)

    def _on_library_change(self, what: str) -> None:
        if self._detached:
            return
        if what == "excerpts":
            self.commonplace.reload()
        elif what == "tables":
            self.table_page.reload()
        self._refresh_timer.start(0)

    def _on_key(self, key) -> None:
        if key != self._key:
            self._key = key
            self.view.verticalScrollBar().setValue(0)
            self.filter_box.blockSignals(True)
            self.filter_box.clear()  # a filter belongs to the place it was typed in
            self.filter_box.blockSignals(False)
        self._show()

    def refresh(self) -> None:
        if self._detached:
            return
        self.tree.rebuild()
        key = self.tree.current_key()
        if key:
            self._key = key
        self._show()

    # -- the shelf tools -------------------------------------------------------------

    def browser(self):
        """Whichever of the painted bookcase and the list is showing."""
        return self.list if self.list_button.isChecked() else self.view

    def _sort_chosen(self, _index: int) -> None:
        prefs.put("shelves/sort", self.sort_box.currentData())
        self._show()

    def _size_chosen(self, height: int) -> None:
        self.view.set_cover_height(height)
        prefs.put("shelves/cover_height", height)

    def _view_chosen(self, as_list: bool) -> None:
        prefs.put("shelves/view", "list" if as_list else "covers")
        self.size_slider.setEnabled(not as_list)
        self._show()

    def set_cover_height(self, height: int) -> None:
        """Change the cover size from elsewhere (the Settings window)."""
        self.size_slider.setValue(height)

    def _refresh_labels(self) -> None:
        """Keep the Label filter's choices in step with the labels that exist."""
        wanted = ["All labels", *self.library.all_labels()]
        current = [self.label_box.itemText(i) for i in range(self.label_box.count())]
        if current != wanted:
            chosen = self.label_box.currentText()
            self.label_box.clear()
            self.label_box.addItems(wanted)
            self.label_box.setCurrentIndex(max(0, self.label_box.findText(chosen)))

    def _present(self, sections: list, hint: str | None = None) -> tuple[int, int]:
        """Filter, sort and show sections. Returns (tomes shown, tomes before filtering)."""
        total = count(sections)
        label = self.label_box.currentText() if self.label_box.currentIndex() > 0 else None
        text = self.filter_box.text()
        labels = self.library.labels_by_book()
        shown = arrange(sections, text, label, self.sort_box.currentData(), labels)
        filtering = bool(text.strip()) or label is not None
        if filtering and count(shown) == 0:
            shown, hint = [], "No tome here matches the filter."
        self.shelves.setCurrentWidget(self.browser())
        if self.browser() is self.list:
            self.list.set_sections(shown, hint, labels)
        else:
            self.view.set_sections(shown, hint)
        return count(shown), total

    def _groups(self, shelf) -> list[Group]:
        """A shelf's loose tomes followed by each of its sets."""
        library = self.library
        groups = [Group(shelf.id, None, library.books_in(shelf.id))]
        for a_set in library.children(shelf.id):
            groups.append(Group(a_set.id, a_set.name, library.books_in(a_set.id)))
        return groups

    def _show(self) -> None:
        library = self.library
        place = self.current_place()
        self.new_child.setVisible(False)
        on_shelves = self._key[0] == "place" or self._key in (
            ("virtual", "desk"), ("virtual", "cart"), ("virtual", "stacks"))
        self.add_button.setVisible(on_shelves)  # adding tomes only makes sense among shelves
        self.tools.setVisible(on_shelves)
        self.size_slider.setEnabled(not self.list_button.isChecked())
        self._refresh_labels()
        if self._key == ("virtual", "catalogue"):
            self.stack.setCurrentWidget(self.catalogue)
            self.heading.setText("Card Catalogue")
            self.subheading.setText("Search the text of every tome in the library")
            if not self.catalogue.field.text().strip():
                self.catalogue.search()  # refreshes the "N tomes" line
            return
        if self._key == ("virtual", "commonplace"):
            self.stack.setCurrentWidget(self.commonplace)
            count = len(self.library.excerpts())
            self.heading.setText("Commonplace Book")
            self.subheading.setText(
                f"Excerpts clipped from your tomes · {count} kept" if count
                else "Excerpts clipped from your tomes")
            return
        if self._key[0] == "table":
            table = library.table(self._key[1])
            if table is not None:
                if self.table_page.table_id != table.id:
                    self.table_page.show_table(table.id)
                self.stack.setCurrentWidget(self.table_page)
                count = len(library.pins(table.id))
                self.heading.setText(table.name)
                self.subheading.setText(
                    f"Study Table · {count} pin{'s' if count != 1 else ''}")
                return
            self._key = ("virtual", "stacks")
        self.stack.setCurrentWidget(self.shelves)
        if place is None:
            name = self._key[1] if self._key[0] == "virtual" else "stacks"
            self._key = ("virtual", name)
            title, subtitle, hint = VIRTUAL_TITLES[name]
            books = {"desk": library.recent_books, "cart": library.unshelved_books,
                     "stacks": library.all_books}[name]()
            sections = [Section(None, "", [Group(None, None, books)], hint)] if books else []
            self._set_heading(title, subtitle, *self._present(sections, hint))
            return

        chain = library.path(place.id)
        trail = " ▸ ".join(p.name for p in chain[:-1])
        if place.kind == "bookcase":
            sections = []
            loose = library.books_in(place.id)
            shelves = library.children(place.id)
            if loose or not shelves:
                sections.append(Section(
                    place.id, "", [Group(place.id, None, loose)],
                    "This bookcase is bare. Add a shelf, or drop tomes here."))
            for shelf in shelves:
                sections.append(Section(shelf.id, shelf.name, self._groups(shelf),
                                        collapsible=True, collapsed=shelf.collapsed))
            subtitle = "Bookcase"
        elif place.kind == "shelf":
            sections = [Section(place.id, place.name, self._groups(place))]
            subtitle = f"Shelf in {trail}"
        else:
            sections = [Section(place.id, place.name,
                                [Group(place.id, None, library.books_in(place.id))],
                                "This set awaits its first tome.")]
            subtitle = f"Set in {trail}"
        self._set_heading(place.name, subtitle, *self._present(sections))
        child = CHILD_KIND.get(place.kind)
        if child:
            self.new_child.setText(f"New {KIND_LABEL[child]}")
            self.new_child.setToolTip(f"Add a {child} to this {place.kind}")
            self.new_child.setVisible(True)

    def _set_heading(self, title: str, subtitle: str, shown: int, total: int | None = None) -> None:
        self.heading.setText(title)
        total = shown if total is None else total
        if shown != total:
            tally = f"showing {shown} of {total} tomes"
        else:
            tally = "no tomes" if total == 0 else f"{total} tome{'s' if total != 1 else ''}"
        self.subheading.setText(f"{subtitle} · {tally}")

    # -- creating and removing places ------------------------------------------------

    def create_place(self, kind: str, parent_id: int | None = None) -> None:
        place = dialogs.create_place(self, self.library, kind, parent_id)
        if place is not None:
            self.refresh()
            self.tree.select_key(("place", place.id))

    def create_table(self) -> None:
        name = dialogs.ask_name(self, "New Study Table", "Name for the new study table:")
        if name is None:
            return
        try:
            table = self.library.add_table(name)
        except LibraryError as problem:
            dialogs.complain(self, str(problem))
            return
        self.refresh()
        self.tree.select_key(("table", table.id))

    def _delete_table(self, table_id: int) -> None:
        table = self.library.table(table_id)
        if table is None:
            return
        if dialogs.confirm(self, "Remove Study Table",
                           f"Remove the study table “{table.name}” and its pins?\n\n"
                           "The tomes themselves are not affected.", "Remove Table"):
            self.library.delete_table(table_id)

    def _new_child_clicked(self) -> None:
        place = self.current_place()
        if place is not None and place.kind in CHILD_KIND:
            self.create_place(CHILD_KIND[place.kind], place.id)

    def _delete_place(self, place_id: int) -> None:
        place = self.library.place(place_id)
        if place is None:
            return
        count = len(self.library.books_in(place_id, recursive=True))
        inside = ""
        if self.library.children(place_id):
            inside = " and everything inside it"
        fate = ""
        if count:
            fate = (f"\n\nIts {count} tome{'s' if count != 1 else ''} stay in the library; any "
                    "that are not shelved elsewhere go to the Returns Cart.")
        if dialogs.confirm(self, f"Remove {KIND_LABEL[place.kind]}",
                           f"Remove the {place.kind} “{place.name}”{inside}?{fate}",
                           f"Remove {KIND_LABEL[place.kind]}"):
            self.library.delete_place(place_id)

    # -- shelving ------------------------------------------------------------------------

    def _books_dropped(self, items: list, to_place_id: int, copy: bool) -> None:
        for book_id, from_place_id in items:
            if copy or from_place_id is None:
                self.library.place_book(book_id, to_place_id)
            else:
                self.library.move_book(book_id, from_place_id, to_place_id)
        target = self.library.place(to_place_id)
        if target is not None:
            noun = "tome" if len(items) == 1 else "tomes"
            self.message.emit(f"Shelved {len(items)} {noun} in “{target.name}”.")

    def _take_off_shelf(self, keys: list) -> None:
        for place_id, book_id in keys:
            if place_id is not None:
                self.library.unplace_book(book_id, place_id)

    def _remove_from_library(self, books: list) -> None:
        if len(books) == 1:
            what = f"“{books[0].title}”"
        else:
            what = f"these {len(books)} tomes"
        if not dialogs.confirm(
                self, "Remove from Library",
                f"Remove {what} from the library?\n\nBookwyrm's copy is deleted, along with its "
                "ribbons and notes. The original file you imported is not touched.",
                "Remove"):
            return
        for book in books:
            self.importer.forget(book.id)
            self.aboutToRemove.emit(book.id)
            self.library.delete_book(book.id)

    # -- importing -------------------------------------------------------------------------

    def add_tomes(self, place_id: int | None = None, paths: list[str] | None = None) -> None:
        """Pick PDFs (unless given) and a destination, then import them."""
        if paths is None:
            settings = QSettings()
            start = settings.value("import_dir", str(Path.home()))
            paths, _ = QFileDialog.getOpenFileNames(self, "Add tomes", start, "PDF files (*.pdf)")
            if not paths:
                return
            settings.setValue("import_dir", str(Path(paths[0]).parent))
        if place_id is None:
            place = self.current_place()
            place_id = place.id if place is not None else None
        dialog = dialogs.ImportDialog(self.library, paths, place_id, self)
        if dialog.exec():
            self.importer.import_files(paths, dialog.place_id())

    def _files_dropped(self, paths: list, place_id) -> None:
        if place_id is not None:
            self.importer.import_files(paths, place_id)  # dropped on a shelf: that is the answer
        else:
            self.add_tomes(paths=paths)

    def _batch_finished(self, summary: dict) -> None:
        added, dupes, failed = summary["added"], summary["duplicates"], summary["failed"]
        parts = []
        if added:
            parts.append(f"{len(added)} tome{'s' if len(added) != 1 else ''} added")
        if dupes:
            parts.append(f"{len(dupes)} already in the library")
        if failed:
            parts.append(f"{len(failed)} could not be read")
        self.message.emit("Import finished: " + ", ".join(parts) + ".")
        if failed:
            lines = "\n".join(f"• {Path(path).name}: {reason}" for path, reason in failed[:12])
            dialogs.complain(self, f"These files could not be added:\n\n{lines}")

    # -- menus ---------------------------------------------------------------------------------

    def _place_menu(self, menu: QMenu, place_id: int) -> None:
        place = self.library.place(place_id)
        if place is None:
            return
        menu.addAction("Add Tomes Here…", lambda: self.add_tomes(place.id))
        child = CHILD_KIND.get(place.kind)
        if child:
            menu.addAction(f"New {KIND_LABEL[child]}…", lambda: self.create_place(child, place.id))
        if place.kind == "shelf":
            menu.addAction("Expand Shelf" if place.collapsed else "Minimize Shelf",
                           lambda: self.library.set_collapsed([place.id], not place.collapsed))
        elif place.kind == "bookcase":
            shelves = [shelf.id for shelf in self.library.children(place.id)]
            if shelves:
                menu.addAction("Minimize All Shelves",
                               lambda: self.library.set_collapsed(shelves, True))
                menu.addAction("Expand All Shelves",
                               lambda: self.library.set_collapsed(shelves, False))
        menu.addSeparator()
        menu.addAction("Rename", lambda: self.tree.edit_place(place.id))
        menu.addAction(f"Remove {KIND_LABEL[place.kind]}…", lambda: self._delete_place(place.id))

    def _tree_menu(self, key, pos) -> None:
        menu = QMenu(self)
        if key and key[0] == "place":
            self._place_menu(menu, key[1])
            menu.addSeparator()
        elif key and key[0] == "table":
            menu.addAction("Rename", lambda: self.tree.edit_key(key))
            menu.addAction("Remove Study Table…", lambda: self._delete_table(key[1]))
            menu.addSeparator()
        menu.addAction("New Bookcase…", lambda: self.create_place("bookcase"))
        menu.addAction("New Study Table…", lambda: self.create_table())
        menu.exec(pos)

    def _shelve_menu(self, menu: QMenu, title: str, handler) -> None:
        """A Bookcase ▸ Shelf ▸ Set submenu; `handler(place_id)` runs on a choice."""
        places = self.library.places()
        if not places:
            return
        by_parent: dict = {}
        for place in places:
            by_parent.setdefault(place.parent_id, []).append(place)
        top = menu.addMenu(title)

        def fill(target: QMenu, parent_id) -> None:
            for place in by_parent.get(parent_id, []):
                children = by_parent.get(place.id)
                if children:
                    sub = target.addMenu(place.name)
                    sub.addAction(f"On “{place.name}” itself",
                                  lambda p=place.id: handler(p))
                    sub.addSeparator()
                    fill(sub, place.id)
                else:
                    target.addAction(place.name, lambda p=place.id: handler(p))

        fill(top, None)

    def _view_menu(self, slot_key, place_id, pos) -> None:
        menu = QMenu(self)
        books = self.browser().selected_books()
        keys = self.browser().selected()
        if slot_key is not None and books:
            ready = [b for b in books if b.ready]
            if ready:
                label = "Open" if len(ready) == 1 else f"Open {len(ready)} Tomes"
                menu.addAction(label, lambda: [self.openBook.emit(b.id) for b in ready])
            if len(books) == 1:
                menu.addAction("Details…", lambda: self._details(books[0]))
            menu.addSeparator()

            def move_to(target):
                self._books_dropped([(book_id, source) for source, book_id in keys], target, False)

            def also_in(target):
                self._books_dropped([(book_id, source) for source, book_id in keys], target, True)

            self._shelve_menu(menu, "Move To", move_to)
            self._shelve_menu(menu, "Also Shelve In", also_in)
            if any(source is not None for source, _ in keys):
                menu.addAction("Take Off This Shelf", lambda: self._take_off_shelf(keys))
            self._labels_menu(menu, books)
            menu.addSeparator()
            if ready:
                menu.addAction("Export a Copy…", lambda: self.export(ready))
                if len(ready) == 1 and self.library.archive_file(ready[0].id).exists():
                    menu.addAction("Export the Original…",
                                   lambda: self.export(ready, original=True))
                if len(ready) == 1:
                    menu.addAction(f"Show in {system.FILE_MANAGER}",
                                   lambda: system.reveal(self.library.book_file(ready[0])))
            menu.addSeparator()
            menu.addAction("Remove from Library…", lambda: self._remove_from_library(books))
        elif place_id is not None:
            self._place_menu(menu, place_id)
        else:
            menu.addAction("Add Tomes…", lambda: self.add_tomes())
            menu.addAction("New Bookcase…", lambda: self.create_place("bookcase"))
        menu.exec(pos)

    def _labels_menu(self, menu: QMenu, books: list) -> None:
        """A Labels submenu: tick or untick each label for the selected tomes."""
        by_book = self.library.labels_by_book()
        sub = menu.addMenu("Labels")
        for name in self.library.all_labels():
            on_all = all(name in by_book.get(book.id, ()) for book in books)
            action = sub.addAction(name)
            action.setCheckable(True)
            action.setChecked(on_all)
            action.triggered.connect(
                lambda _checked=False, n=name, add=not on_all: self._label(books, n, add))
        if self.library.all_labels():
            sub.addSeparator()
        sub.addAction("New Label…", lambda: self._new_label(books))

    def _label(self, books: list, name: str, on: bool) -> None:
        for book in books:
            current = self.library.labels_of(book.id)
            kept = [n for n in current if n.casefold() != name.casefold()]
            self.library.set_labels(book.id, kept + ([name] if on else []))

    def _new_label(self, books: list) -> None:
        name = dialogs.ask_name(self, "New Label", "Label to give these tomes:")
        if name:
            self._label(books, name, True)

    def export(self, books: list, original: bool = False) -> None:
        """Save copies of tomes outside the library: the current file, or the first version."""
        settings = QSettings()
        start = Path(settings.value("export_dir", str(Path.home())))

        def source(book) -> Path:
            return self.library.archive_file(book.id) if original else \
                self.library.book_file(book)

        def name(book) -> str:
            return safe_filename(book.title + (" (original)" if original else "") + ".pdf")

        if len(books) == 1:
            chosen, _ = QFileDialog.getSaveFileName(
                self, "Export a copy", str(start / name(books[0])), "PDF files (*.pdf)")
            if not chosen:
                return
            targets = [(books[0], Path(chosen))]
        else:
            folder = QFileDialog.getExistingDirectory(self, "Export copies into", str(start))
            if not folder:
                return
            targets = []
            for book in books:
                target = Path(folder) / name(book)
                number = 2
                while target.exists() or target in [t for _, t in targets]:
                    target = Path(folder) / f"{Path(name(book)).stem} ({number}).pdf"
                    number += 1
                targets.append((book, target))
        try:
            for book, target in targets:
                shutil.copyfile(source(book), target)
        except OSError as problem:
            dialogs.complain(self, f"The copy could not be written.\n\n{problem}")
            return
        settings.setValue("export_dir", str(targets[0][1].parent))
        noun = "tome" if len(targets) == 1 else "tomes"
        note = f"Exported {len(targets)} {noun} to {targets[0][1].parent}."
        if not original and any(self.unsaved(book.id) for book, _ in targets):
            note += " Unsaved changes are not in the copy; save the tome and export again."
        self.message.emit(note)

    def _details(self, book) -> None:
        try:
            dialogs.TomeDetailsDialog(self.library, book, self).exec()
        except LibraryError as problem:
            dialogs.complain(self, str(problem))
