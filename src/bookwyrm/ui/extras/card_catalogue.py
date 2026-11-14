"""The Card Catalogue: search the text of the library, tome by tome.

Every tome that mentions the words is listed with the number of its pages that
do and the best few of them; "Show all" under a tome lists the rest. The search
can be narrowed to chosen bookcases, shelves, sets and tomes (see search_scope).

Rows are painted by a delegate rather than built from widgets, and excerpts are
made only for rows that come into view, so a search that matches thousands of
pages costs no more to show than one that matches ten.
"""

from __future__ import annotations

import html
import re
import time

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookwyrm.db.repo import Library
from bookwyrm.ui import prefs
from bookwyrm.ui.extras import search_scope
from bookwyrm.ui.extras.search_scope import Scope, ScopePicker
from bookwyrm.ui.keys import menu_text
from bookwyrm.ui.theme import icons, palette

ROLE = Qt.ItemDataRole.UserRole  # (book id, 0-based page, text to highlight): where a click leads
KIND = ROLE + 1  # "tome" | "page" | "more"
INFO = ROLE + 2  # tome: (title, pages matching) · page: its row in the index
#                  more: (book id, pages matching, showing all?)
SNIPPET = ROLE + 3  # a page row's excerpt; None until it has been made

PER_TOME = 6  # pages listed under each tome before "Show all"
EAGER_ROWS = 48  # excerpts made straight away, so the first screenful appears complete
TOME_H, PAGE_H, MORE_H = 58, 34, 30
COVER_H = 44
ORDERS = (("best", "Best match first"), ("pages", "Most pages first"),
          ("title", "Title, A to Z"))


def snippet_html(snippet: str) -> str:
    """FTS marks matches with \\x02...\\x03; show them in candle-amber."""
    text = html.escape(re.sub(r"\s+", " ", snippet).strip())
    text = text.replace("\x02", f'<b style="color:{palette.AMBER_400}">').replace("\x03", "</b>")
    return text


def segments(snippet: str) -> list[tuple[str, bool]]:
    """An excerpt as (text, is a match) pieces, in order."""
    pieces: list[tuple[str, bool]] = []
    for index, chunk in enumerate(re.sub(r"\s+", " ", snippet).strip().split("\x02")):
        if index == 0:
            if chunk:
                pieces.append((chunk, False))
            continue
        match, _, rest = chunk.partition("\x03")
        if match:
            pieces.append((match, True))
        if rest:
            pieces.append((rest, False))
    return pieces


def lead_in(pieces: list[tuple[str, bool]], metrics: QFontMetrics,
            room: int) -> list[tuple[str, bool]]:
    """Shorten the words before an excerpt's first match so the match itself is in
    view: a row shows one line, and what does not fit is cut off at the right."""
    if len(pieces) < 2 or pieces[0][1]:
        return pieces
    limit = max(0, int(room * 0.4))
    lead = pieces[0][0]
    if metrics.horizontalAdvance(lead) <= limit:
        return pieces
    kept: list[str] = []
    width = metrics.horizontalAdvance("…")
    for word in reversed(lead.split(" ")):
        width += metrics.horizontalAdvance(word + " ")
        if width > limit:
            break
        kept.append(word)
    return [("…" + " ".join(reversed(kept)), False), *pieces[1:]]


def find_needle(query: str, together: bool = False) -> str:
    """What to highlight inside a tome for a catalogue query.

    Find-in-tome matches one literal phrase, so use the first quoted phrase; else
    the whole query if it is one word, or if its words stand `together` on the
    page being opened; else its longest word.
    """
    phrase = re.search(r'"([^"]+)"', query)
    if phrase:
        return phrase.group(1).strip()
    words = re.findall(r"\w[\w'’-]*", query)
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return " ".join(query.split()) if together else max(words, key=len)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


class ResultDelegate(QStyledItemDelegate):
    """Paints the three kinds of row: a tome, one of its pages, and "Show all"."""

    def __init__(self, owner: CardCatalogue):
        super().__init__(owner)
        self.owner = owner

    def sizeHint(self, option, index) -> QSize:
        kind = index.data(KIND)
        return QSize(0, TOME_H if kind == "tome" else PAGE_H if kind == "page" else MORE_H)

    def paint(self, painter, option, index) -> None:
        plain = QStyleOptionViewItem(option)
        self.initStyleOption(plain, index)
        plain.text = ""
        style = plain.widget.style() if plain.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, plain, painter, plain.widget)
        painter.save()
        painter.setClipRect(option.rect)
        area = option.rect.adjusted(6, 0, -14, 0)
        kind = index.data(KIND)
        if kind == "tome":
            self._tome(painter, option, area, index)
        elif kind == "page":
            self._page(painter, option, area, index)
        else:
            self._more(painter, option, area, index)
        painter.restore()

    @staticmethod
    def _text(painter, x: int, area: QRect, text: str, font: QFont, color: str) -> int:
        """Draw one run of text, cut short with an ellipsis if it will not fit.
        Returns the x just past it, or -1 if the row is full."""
        room = area.right() - x
        if room <= 0:
            return -1
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text)
        full = width > room
        if full:
            text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, room)
            width = room
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QRect(x, area.top(), width + 2, area.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                         | Qt.TextFlag.TextSingleLine, text)
        return -1 if full else x + width

    def _tome(self, painter, option, area: QRect, index) -> None:
        title, total = index.data(INFO)
        x = area.left()
        cover = self.owner.cover(index.data(ROLE)[0])
        if cover is not None:
            size = cover.deviceIndependentSize()
            painter.drawPixmap(x + round((34 - size.width()) / 2),
                               area.top() + round((area.height() - size.height()) / 2), cover)
        x += 44
        font = QFont(option.font)
        font.setPixelSize(17)
        count = QFont(option.font)
        count.setItalic(True)
        label = _plural(total, "page")
        reserve = QFontMetrics(count).horizontalAdvance(label) + 18
        fits = QRect(area.left(), area.top(), max(60, area.width() - reserve), area.height())
        x = self._text(painter, x, fits, title, font, palette.SILVER_100)
        x = fits.right() + 2 if x < 0 else x + 14
        self._text(painter, x, area, label, count, palette.SILVER_500)

    def _page(self, painter, option, area: QRect, index) -> None:
        font = QFont(option.font)
        strong = QFont(font)
        strong.setBold(True)
        page = index.data(ROLE)[1]
        column = QFontMetrics(font).horizontalAdvance("p. 0000") + 14
        self._text(painter, area.left(), area, f"p. {page + 1}", font, palette.FROST_300)
        snippet = index.data(SNIPPET)
        if snippet is None:
            self.owner.want_excerpt(index)  # made after this paint; the row is redrawn then
            return
        x = area.left() + column
        for text, matched in lead_in(segments(snippet), QFontMetrics(font), area.right() - x):
            x = self._text(painter, x, area, text, strong if matched else font,
                           palette.AMBER_400 if matched else palette.SILVER_300)
            if x < 0:
                break

    def _more(self, painter, option, area: QRect, index) -> None:
        _book, total, showing_all = index.data(INFO)
        font = QFont(option.font)
        font.setItalic(True)
        text = ("Show only the best matches" if showing_all
                else f"Show all {_plural(total, 'page')} in this tome")
        self._text(painter, area.left(), area, text, font, palette.FROST_500)


class CardCatalogue(QWidget):
    openRequested = Signal(int, int, str)  # book id, 0-based page, text to highlight

    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self.library = library
        self._scope = Scope()  # where to search; empty means every tome
        self._query = ""  # the words the listed results answer
        self._found: dict = {}  # book id -> TomeHits of the listed results
        self._wanted: dict[int, QTreeWidgetItem] = {}  # page rows waiting for an excerpt
        self._covers: dict[int, QPixmap | None] = {}
        self._took = 0.0  # how long the last search took, to pace searching while typing

        self.field = QLineEdit(self)
        self.field.setClearButtonEnabled(True)
        self.field.addAction(icons.icon("magnifying-glass"),
                             QLineEdit.ActionPosition.LeadingPosition)
        self.field.setMinimumHeight(36)
        self.field.installEventFilter(self)

        self.scope_button = QPushButton(self)
        self.scope_button.setObjectName("scopeButton")
        self.scope_button.setIcon(icons.icon("bookshelf"))
        self.scope_button.setToolTip(
            "Narrow the search to chosen bookcases, shelves, sets or single tomes")
        self.scope_button.clicked.connect(self.choose_scope)
        self.everywhere = QToolButton(self)
        self.everywhere.setText("Search Every Tome")
        self.everywhere.setToolTip("Stop narrowing: search the whole library again")
        self.everywhere.clicked.connect(lambda: self.set_scope(Scope()))
        self.order_box = QComboBox(self)
        self.order_box.setToolTip("The order tomes are listed in")
        for key, label in ORDERS:
            self.order_box.addItem(label, key)
        self.order_box.setCurrentIndex(
            max(0, self.order_box.findData(prefs.get("catalogue/order"))))
        self.order_box.activated.connect(self._order_chosen)
        self.picker = ScopePicker(library, self)
        self.picker.changed.connect(lambda: self.set_scope(self.picker.scope()))

        self.summary = QLabel("", self)
        self.summary.setObjectName("subheading")

        self.results = QTreeWidget(self)
        self.results.setHeaderHidden(True)
        self.results.setIndentation(18)
        self.results.setUniformRowHeights(False)
        self.results.setExpandsOnDoubleClick(False)
        self.results.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.results.setItemDelegate(ResultDelegate(self))
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._context)
        self.results.itemClicked.connect(self._activated)
        self.results.installEventFilter(self)

        top = QHBoxLayout()
        top.setContentsMargins(22, 14, 22, 6)
        top.addWidget(self.field, 1)
        narrow = QHBoxLayout()
        narrow.setContentsMargins(22, 0, 22, 8)
        narrow.setSpacing(8)
        narrow.addWidget(self.scope_button)
        narrow.addWidget(self.everywhere)
        narrow.addStretch(1)
        narrow.addWidget(self.order_box)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addLayout(narrow)
        self.summary.setContentsMargins(24, 0, 24, 8)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)

        self._timer = QTimer(self, singleShot=True, interval=180)
        self._timer.timeout.connect(self.search)
        self._excerpts = QTimer(self, singleShot=True, interval=0)
        self._excerpts.timeout.connect(self._load_excerpts)
        self.field.textChanged.connect(self._typed)
        self.field.returnPressed.connect(self.search)
        self._show_scope()
        self._show_idle()

    # -- where to search ----------------------------------------------------------------

    def scope(self) -> Scope:
        return self._scope

    def set_scope(self, scope: Scope) -> None:
        """Narrow the search (an empty Scope searches every tome) and search again."""
        scope = search_scope.still_valid(self.library, scope)
        changed = scope != self._scope
        self._scope = scope
        self._show_scope()
        if changed or not self._query:
            self.search()

    def choose_scope(self) -> None:
        if time.monotonic() - self.picker.closed_at < 0.25:
            return  # this click is the one that just closed the picker
        self.picker.open_below(self.scope_button, self._scope)

    def scope_words(self) -> str:
        return search_scope.describe(self.library, self._scope)

    def _where(self) -> str:
        """The scope as it reads inside a sentence: 'the library', 'the D&D bookcase',
        'Out of the Abyss', 'the 3 chosen tomes'."""
        if not self._scope:
            return "the library"
        words = self.scope_words()
        return f"the {words}" if self._several_chosen(words) else words

    @staticmethod
    def _several_chosen(words: str) -> bool:
        return words.endswith(("chosen tome", "chosen tomes"))

    def _show_scope(self) -> None:
        narrowed = bool(self._scope)
        words = self.scope_words()
        self.scope_button.setText(
            "Search in: " + ("Every Tome" if not narrowed else menu_text(words)))
        self.scope_button.setProperty("narrowed", narrowed)
        self.scope_button.style().unpolish(self.scope_button)  # restyle for the property
        self.scope_button.style().polish(self.scope_button)
        self.everywhere.setVisible(narrowed)
        self.field.setPlaceholderText(f'Search {words}…  (use "quotes" for an exact phrase)')

    def refresh(self) -> None:
        """The library changed, or this page is being shown: bring names and counts up to date."""
        scope = search_scope.still_valid(self.library, self._scope)
        if scope != self._scope:
            self.set_scope(scope)
            return
        self._show_scope()
        if not self._query:
            self._show_idle()

    def library_changed(self, what: str) -> None:
        if what == "books":
            self._covers.clear()  # a cover may have been redrawn, or its tome removed

    # -- searching ----------------------------------------------------------------------

    def focus_field(self) -> None:
        self.field.setFocus()
        self.field.selectAll()

    def _typed(self, _text: str) -> None:
        # A search that took long (a huge library, a very common word) waits for a longer pause.
        self._timer.start(180 if self._took < 0.06 else 420)

    def _order_chosen(self, _index: int) -> None:
        prefs.put("catalogue/order", self.order_box.currentData())
        if self._query:
            self.search()

    def _clear(self) -> None:
        self._wanted.clear()
        self.results.clear()
        self._found = {}
        self._query = ""

    def _show_idle(self) -> None:
        self._clear()
        books = self.library.all_books()
        chosen = search_scope.books_in(self.library, self._scope)
        within = books if chosen is None else [b for b in books if b.id in chosen]
        waiting = sum(1 for b in within if b.ready and not b.indexed)
        if not books:
            self.summary.setText("The catalogue is empty until the library holds some tomes.")
            return
        if chosen is None:
            text = f"Searches the text of all {_plural(len(books), 'tome')} in the library."
        elif not within:
            text = f"There are no tomes in {self._where()} to search."
        elif len(self._scope.books) == 1 and len(within) == 1:
            text = f"Searches the text of {within[0].title}."
        elif self._several_chosen(self.scope_words()):
            text = f"Searches the text of {self._where()}."
        else:
            text = (f"Searches the text of the {_plural(len(within), 'tome')} "
                    f"in {self._where()}.")
        if waiting:
            text += (f" The Librarian is still reading {waiting} of them; their pages appear "
                     "here as they are finished.")
        self.summary.setText(text)

    def search(self) -> None:
        self._timer.stop()
        query = self.field.text().strip()
        if len(query) < 2:
            self._show_idle()
            return
        started = time.perf_counter()
        self._scope = search_scope.still_valid(self.library, self._scope)
        self._show_scope()
        chosen = search_scope.books_in(self.library, self._scope)
        books = {book.id: book for book in self.library.all_books()}
        tomes = [tome for tome in self.library.search_tomes(query, chosen, PER_TOME)
                 if tome.book_id in books]
        self._clear()
        where = self._where()
        if not tomes:
            self.summary.setText(f"No page in {where} mentions “{query}”.")
            self._took = time.perf_counter() - started
            return
        order = self.order_box.currentData()
        if order == "pages":
            tomes.sort(key=lambda t: (-t.total, books[t.book_id].title.casefold()))
        elif order == "title":
            tomes.sort(key=lambda t: books[t.book_id].title.casefold())
        self._query = query
        self._found = {tome.book_id: tome for tome in tomes}
        self.results.setUpdatesEnabled(False)
        for tome in tomes:
            item = QTreeWidgetItem(self.results)
            item.setData(0, KIND, "tome")
            item.setData(0, ROLE, (tome.book_id, tome.best.page, self._needle(tome.best)))
            item.setData(0, INFO, (books[tome.book_id].title, tome.total))
            self._list_pages(item, tome.pages, tome.total, showing_all=False)
            item.setExpanded(True)
        self.results.setUpdatesEnabled(True)
        self._first_excerpts()
        pages = sum(tome.total for tome in tomes)
        narrowed = f" · searching {where}" if self._scope else ""
        self.summary.setText(
            f"{_plural(pages, 'page')} in {_plural(len(tomes), 'tome')}{narrowed}. "
            "Click a page to open it there.")
        self._took = time.perf_counter() - started

    def _needle(self, hit) -> str:
        return find_needle(self._query, hit.phrase)

    def _list_pages(self, tome_item, pages, total: int, showing_all: bool) -> None:
        """Put page rows under a tome's row, replacing any already there."""
        self._wanted.clear()  # rows are about to go; what is still on screen asks again
        tome_item.takeChildren()
        book_id = tome_item.data(0, ROLE)[0]
        for hit in pages:
            row = QTreeWidgetItem(tome_item)
            row.setData(0, KIND, "page")
            row.setData(0, ROLE, (book_id, hit.page, self._needle(hit)))
            row.setData(0, INFO, hit.rowid)
        if total > PER_TOME and (showing_all or total > len(pages)):
            more = QTreeWidgetItem(tome_item)
            more.setData(0, KIND, "more")
            more.setData(0, INFO, (book_id, total, showing_all))

    def _show_all_or_best(self, more_item) -> None:
        """The row under a tome that swaps between its best pages and all of them."""
        book_id, total, showing_all = more_item.data(0, INFO)
        tome_item = more_item.parent()
        if showing_all:
            found = self._found.get(book_id)
            pages = found.pages if found else ()
        else:
            pages = self.library.search_tome(self._query, book_id)
            total = max(total, len(pages))
        self._list_pages(tome_item, pages, total, showing_all=not showing_all)
        if showing_all:
            self.results.scrollToItem(tome_item)

    # -- excerpts ---------------------------------------------------------------------------

    def _page_rows(self):
        for i in range(self.results.topLevelItemCount()):
            tome = self.results.topLevelItem(i)
            for j in range(tome.childCount()):
                row = tome.child(j)
                if row.data(0, KIND) == "page":
                    yield row

    def _first_excerpts(self) -> None:
        for count, row in enumerate(self._page_rows()):
            if count >= EAGER_ROWS:
                break
            self._wanted[row.data(0, INFO)] = row
        self._load_excerpts()

    def want_excerpt(self, index) -> None:
        """A page row without its excerpt is being painted: make it shortly."""
        row = self.results.itemFromIndex(index)
        if row is not None and row.data(0, INFO) not in self._wanted:
            self._wanted[row.data(0, INFO)] = row
            self._excerpts.start(0)

    def _load_excerpts(self) -> None:
        wanted, self._wanted = self._wanted, {}
        if not wanted or not self._query:
            return
        found = self.library.snippets(self._query, wanted)
        for rowid, row in wanted.items():
            text = found.get(rowid, "")  # "" also stops the row from asking again
            row.setData(0, SNIPPET, text)
            if text:
                row.setToolTip(0, f"<p>{snippet_html(text)}</p>")

    def load_all_excerpts(self) -> None:
        """Make every listed row's excerpt now (the screen does this as rows come into view)."""
        for row in self._page_rows():
            if row.data(0, SNIPPET) is None:
                self._wanted[row.data(0, INFO)] = row
        self._load_excerpts()

    def cover(self, book_id: int) -> QPixmap | None:
        """A tome's cover at the size of a result row, loaded when first painted."""
        if book_id not in self._covers:
            picture = QPixmap(str(self.library.cover_file(book_id)))
            if picture.isNull():
                self._covers[book_id] = None
            else:
                ratio = self.devicePixelRatioF()
                picture = picture.scaledToHeight(
                    round(COVER_H * ratio), Qt.TransformationMode.SmoothTransformation)
                picture.setDevicePixelRatio(ratio)
                self._covers[book_id] = picture
        return self._covers[book_id]

    # -- acting on a row --------------------------------------------------------------------

    def _activated(self, item, _column=0) -> None:
        if item is None:
            return
        if item.data(0, KIND) == "more":
            self._show_all_or_best(item)
            return
        target = item.data(0, ROLE)
        if target:
            self.openRequested.emit(*target)

    def menu_for(self, item) -> QMenu | None:
        """The right-click menu for a result row."""
        if item is None or item.data(0, KIND) == "more":
            return None
        book_id = item.data(0, ROLE)[0]
        menu = QMenu(self)
        opens = "Open at the Best Match" if item.data(0, KIND) == "tome" else "Open This Page"
        menu.addAction(opens, lambda: self._activated(item))
        only = menu.addAction(
            "Search Only This Tome",
            lambda: self.set_scope(search_scope.scope_of(books=[book_id])))
        only.setEnabled(self._scope != search_scope.scope_of(books=[book_id]))
        return menu

    def _context(self, position) -> None:
        menu = self.menu_for(self.results.itemAt(position))
        if menu is not None:
            menu.exec(self.results.viewport().mapToGlobal(position))

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if watched is self.field and key == Qt.Key.Key_Down:
                if self.results.topLevelItemCount():
                    self.results.setFocus()
                    if self.results.currentItem() is None:
                        self.results.setCurrentItem(self.results.topLevelItem(0))
                    return True
            elif watched is self.results and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._activated(self.results.currentItem())
                return True
        return super().eventFilter(watched, event)
