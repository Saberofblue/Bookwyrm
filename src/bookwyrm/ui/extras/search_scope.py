"""Choosing where the Card Catalogue searches: whole bookcases, shelves and sets,
or tomes picked one by one."""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from bookwyrm.db.repo import Library
from bookwyrm.ui.theme import icons

ROLE = Qt.ItemDataRole.UserRole
KIND_ICON = {"bookcase": "bookshelf", "shelf": "stack", "set": "book-cover"}
KIND_WORD = {"bookcase": "bookcase", "shelf": "shelf", "set": "set"}
CART = ("cart", 0)
Checked, Unchecked = Qt.CheckState.Checked, Qt.CheckState.Unchecked


@dataclass(frozen=True)
class Scope:
    """Where to search. Empty means everywhere.

    A place stands for whatever is inside it at the time of the search, so a tome
    shelved there later is searched too; `books` are tomes chosen by themselves.
    """

    places: frozenset[int] = frozenset()  # bookcases, shelves and sets, with all they hold
    books: frozenset[int] = frozenset()
    cart: bool = False  # the Returns Cart: tomes standing on no shelf

    def __bool__(self) -> bool:
        return bool(self.places or self.books or self.cart)


def scope_of(places=(), books=(), cart: bool = False) -> Scope:
    return Scope(frozenset(places), frozenset(books), bool(cart))


def still_valid(library: Library, scope: Scope) -> Scope:
    """The scope without places and tomes that have since left the library."""
    if not scope:
        return scope
    places = {place.id for place in library.places()}
    books = {book.id for book in library.all_books()}
    return Scope(scope.places & places, scope.books & books, scope.cart)


def books_in(library: Library, scope: Scope) -> set[int] | None:
    """Ids of the tomes a scope covers; None when it covers the whole library."""
    if not scope:
        return None
    books = set(scope.books) | library.books_within(scope.places)
    if scope.cart:
        books.update(book.id for book in library.unshelved_books())
    return books


def describe(library: Library, scope: Scope) -> str:
    """A few words for a scope: 'every tome', 'the D&D bookcase', 'Out of the Abyss',
    '12 chosen tomes'."""
    if not scope:
        return "every tome"
    chosen = len(scope.places) + len(scope.books) + int(scope.cart)
    if chosen == 1:
        if scope.cart:
            return "the Returns Cart"
        if scope.places:
            place = library.place(next(iter(scope.places)))
            if place is not None:
                return f"the {place.name} {KIND_WORD[place.kind]}"
        else:
            book = library.book(next(iter(scope.books)))
            if book is not None:
                return book.title if len(book.title) <= 44 else book.title[:42].rstrip() + "…"
    count = len(books_in(library, scope) or ())
    return f"{count} chosen tome{'s' if count != 1 else ''}"


class ScopePicker(QFrame):
    """A pop-up list of the library with a tick box beside every bookcase, shelf,
    set and tome. Ticking a place ticks everything inside it."""

    changed = Signal()  # the ticks changed (reported once things settle)

    def __init__(self, library: Library, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("scopePicker")
        self.library = library
        self._quiet = False  # True while the ticks are being set by the program
        self._boxed = False  # a tick box itself took the last click
        self._tomes: dict[int, list[QTreeWidgetItem]] = {}  # every row showing each tome
        self.closed_at = 0.0  # when the picker last closed (see CardCatalogue.choose_scope)

        title = QLabel("Search in")
        title.setObjectName("sectionLabel")
        self.find = QLineEdit()
        self.find.setPlaceholderText("Find a bookcase, shelf, set or tome…")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(self._filter)
        self.tree = QTreeWidget()
        self.tree.setObjectName("scopeTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setIndentation(18)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemClicked.connect(self._item_clicked)
        self.empty = QLabel("The library has no tomes yet.")
        self.empty.setObjectName("subheading")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.everything = QPushButton("Search Every Tome")
        self.everything.setToolTip("Untick everything: search the whole library")
        self.everything.clicked.connect(lambda: self.set_scope(Scope(), announce=True))
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.clicked.connect(self.close)
        for button in (self.everything, done):
            button.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.everything)
        buttons.addStretch(1)
        buttons.addWidget(done)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self.find)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.empty)
        layout.addLayout(buttons)
        self.resize(380, 460)

        self._settle = QTimer(self, singleShot=True, interval=120)
        self._settle.timeout.connect(self.changed)

    # -- filling ----------------------------------------------------------------------

    def rebuild(self, scope: Scope | None = None) -> None:
        """Read the library afresh, keeping what is ticked (or ticking `scope`)."""
        scope = self.scope() if scope is None else scope
        library = self.library
        self._quiet = True
        self.tree.clear()
        self._tomes = {}
        by_parent: dict = {}
        for place in library.places():
            by_parent.setdefault(place.parent_id, []).append(place)

        def add_tome(book, parent_item) -> None:
            item = QTreeWidgetItem(parent_item, [book.title])
            item.setData(0, ROLE, ("book", book.id))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Unchecked)
            item.setToolTip(0, book.title)
            self._tomes.setdefault(book.id, []).append(item)

        def add_place(place, parent_item) -> None:
            item = QTreeWidgetItem(parent_item, [place.name])
            item.setIcon(0, icons.icon(KIND_ICON[place.kind]))
            item.setData(0, ROLE, ("place", place.id))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                          | Qt.ItemFlag.ItemIsAutoTristate)
            item.setCheckState(0, Unchecked)
            item.setToolTip(0, f"Everything in this {KIND_WORD[place.kind]}")
            for book in library.books_in(place.id):
                add_tome(book, item)
            for child in by_parent.get(place.id, []):
                add_place(child, item)
            item.setExpanded(place.kind == "bookcase")

        for bookcase in by_parent.get(None, []):
            add_place(bookcase, self.tree.invisibleRootItem())
        loose = library.unshelved_books()
        if loose:
            cart = QTreeWidgetItem(self.tree, ["Returns Cart"])
            cart.setIcon(0, icons.icon("hand-truck"))
            cart.setData(0, ROLE, CART)
            cart.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                          | Qt.ItemFlag.ItemIsAutoTristate)
            cart.setCheckState(0, Unchecked)
            cart.setToolTip(0, "Tomes not yet standing on any shelf")
            for book in loose:
                add_tome(book, cart)
            cart.setExpanded(not by_parent)  # with no bookcases the cart is all there is
        self._quiet = False
        has_rows = self.tree.topLevelItemCount() > 0
        self.tree.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        self._apply(scope)
        self._filter(self.find.text())

    def _rows(self):
        """Every row, parents before their children."""
        pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while pending:
            item = pending.pop(0)
            yield item
            pending[0:0] = [item.child(i) for i in range(item.childCount())]

    # -- what is ticked ---------------------------------------------------------------

    def scope(self) -> Scope:
        """What the ticks mean. A fully ticked place counts as the place itself;
        inside a partly ticked one, each ticked thing counts separately."""
        places: set[int] = set()
        books: set[int] = set()
        cart = False

        def read(item) -> None:
            nonlocal cart
            kind, ident = item.data(0, ROLE)
            state = item.checkState(0)
            if state == Unchecked:
                return
            if kind == "book":
                books.add(ident)
            elif state == Checked:
                if kind == "place":
                    places.add(ident)
                else:
                    cart = True
            else:  # partly ticked: look inside
                for i in range(item.childCount()):
                    read(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            read(self.tree.topLevelItem(i))
        # A tome ticked only because a whole place holding it is ticked is not a separate choice.
        return Scope(frozenset(places), frozenset(books - self._under(places, cart)), cart)

    def _under(self, places: set[int], cart: bool) -> set[int]:
        """Tomes that stand inside the given fully ticked places (or the ticked cart)."""
        covered: set[int] = set()
        for item in self._rows():
            kind, ident = item.data(0, ROLE)
            if (kind == "place" and ident in places) or (kind == "cart" and cart):
                stack = [item]
                while stack:
                    row = stack.pop()
                    for i in range(row.childCount()):
                        child = row.child(i)
                        if child.data(0, ROLE)[0] == "book":
                            covered.add(child.data(0, ROLE)[1])
                        else:
                            stack.append(child)
        return covered

    def set_scope(self, scope: Scope, announce: bool = False) -> None:
        self._apply(scope)
        if announce:
            self._settle.start()

    def _apply(self, scope: Scope) -> None:
        self._quiet = True
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Unchecked)  # children follow
        for item in list(self._rows()):
            kind, ident = item.data(0, ROLE)
            if ((kind == "place" and ident in scope.places) or (kind == "cart" and scope.cart)
                    or (kind == "book" and ident in scope.books)):
                item.setCheckState(0, Checked)
        self._quiet = False
        for book_id, rows in self._tomes.items():  # a tome shelved twice is ticked in both
            if len(rows) > 1 and any(row.checkState(0) == Checked for row in rows):
                self._match_twins(book_id, Checked)
        self._refresh_button()

    def _match_twins(self, book_id: int, state) -> None:
        self._quiet = True
        for row in self._tomes.get(book_id, []):
            if row.checkState(0) != state:
                row.setCheckState(0, state)
        self._quiet = False

    def _item_changed(self, item, _column: int) -> None:
        if self._quiet:
            return
        self._boxed = True
        kind, ident = item.data(0, ROLE)
        if kind == "book" and len(self._tomes.get(ident, ())) > 1:
            self._match_twins(ident, item.checkState(0))
        self._refresh_button()
        self._settle.start()

    def _item_clicked(self, item, _column: int) -> None:
        """A click on a row's name ticks it too, not only a click on the small box."""
        if self._boxed:  # the box itself was clicked and has already changed
            self._boxed = False
            return
        item.setCheckState(0, Unchecked if item.checkState(0) == Checked else Checked)
        self._boxed = False

    def _refresh_button(self) -> None:
        self.everything.setEnabled(any(
            self.tree.topLevelItem(i).checkState(0) != Unchecked
            for i in range(self.tree.topLevelItemCount())))

    # -- finding a row ------------------------------------------------------------------

    def _filter(self, text: str) -> None:
        """Show only rows whose name contains `text`, along with the places that hold
        them and, for a place that matches, everything inside it."""
        needle = text.strip().casefold()

        def visit(item, inside_match: bool) -> bool:
            own = bool(needle) and needle in item.text(0).casefold()
            holds = False
            for i in range(item.childCount()):
                holds = visit(item.child(i), inside_match or own) or holds
            item.setHidden(bool(needle) and not (own or holds or inside_match))
            if needle and holds:
                item.setExpanded(True)
            return own or holds

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i), False)

    # -- showing ----------------------------------------------------------------------

    def open_below(self, anchor, scope: Scope) -> None:
        """Show the picker under `anchor` (the button that opens it), ticked to `scope`."""
        self.find.clear()
        self.rebuild(scope)
        corner = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            corner.setX(max(screen.left(), min(corner.x(), screen.right() - self.width())))
            if corner.y() + self.height() > screen.bottom():
                corner.setY(max(screen.top(), screen.bottom() - self.height()))
        self.move(corner)
        self.show()
        self.find.setFocus()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed_at = time.monotonic()
        if self._settle.isActive():  # report a last-moment change at once
            self._settle.stop()
            self.changed.emit()
