"""The Card Catalogue: search the text of every tome in the library at once."""

from __future__ import annotations

import html
import re

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookwyrm.db.repo import Library
from bookwyrm.ui.theme import icons, palette

ROLE = Qt.ItemDataRole.UserRole
LIMIT = 400


def snippet_html(snippet: str) -> str:
    """FTS marks matches with \\x02...\\x03; show them in candle-amber."""
    text = html.escape(re.sub(r"\s+", " ", snippet).strip())
    text = text.replace("\x02", f'<b style="color:{palette.AMBER_400}">').replace("\x03", "</b>")
    return text


def find_needle(query: str) -> str:
    """What to highlight inside a tome for a catalogue query.

    Find-in-tome matches one literal phrase, so use the first quoted phrase,
    else the whole query if it is a single word, else its longest word.
    """
    phrase = re.search(r'"([^"]+)"', query)
    if phrase:
        return phrase.group(1).strip()
    words = re.findall(r"\w[\w'’-]*", query)
    if not words:
        return ""
    return max(words, key=len) if len(words) > 1 else words[0]


class CardCatalogue(QWidget):
    openRequested = Signal(int, int, str)  # book id, 0-based page, text to highlight

    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self.library = library
        self.field = QLineEdit(self)
        self.field.setPlaceholderText('Search every tome…  (use "quotes" for an exact phrase)')
        self.field.setClearButtonEnabled(True)
        self.field.addAction(icons.icon("magnifying-glass"),
                             QLineEdit.ActionPosition.LeadingPosition)
        self.field.setMinimumHeight(36)
        self.summary = QLabel("", self)
        self.summary.setObjectName("subheading")

        self.results = QTreeWidget(self)
        self.results.setHeaderHidden(True)
        self.results.setIndentation(18)
        self.results.setIconSize(QSize(34, 44))
        self.results.setUniformRowHeights(False)
        self.results.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.results.itemActivated.connect(self._activated)
        self.results.itemClicked.connect(self._activated)

        top = QHBoxLayout()
        top.setContentsMargins(22, 14, 22, 8)
        top.addWidget(self.field, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        self.summary.setContentsMargins(24, 0, 24, 8)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)

        self._timer = QTimer(self, singleShot=True, interval=180)
        self._timer.timeout.connect(self.search)
        self.field.textChanged.connect(lambda _: self._timer.start(180))
        self.field.returnPressed.connect(self.search)
        self._show_idle()

    def focus_field(self) -> None:
        self.field.setFocus()
        self.field.selectAll()

    def _show_idle(self) -> None:
        self.results.clear()
        books = self.library.all_books()
        waiting = sum(1 for b in books if b.ready and not b.indexed)
        if not books:
            self.summary.setText("The catalogue is empty until the library holds some tomes.")
        elif waiting:
            self.summary.setText(
                f"Searches the text of all {len(books)} tomes. The Librarian is still reading "
                f"{waiting} of them; their pages appear here as they are finished.")
        else:
            self.summary.setText(f"Searches the text of all {len(books)} tomes in the library.")

    def search(self) -> None:
        self._timer.stop()
        query = self.field.text().strip()
        if len(query) < 2:
            self._show_idle()
            return
        hits = self.library.search(query, LIMIT)
        self.results.clear()
        if not hits:
            self.summary.setText(f"No page in the library mentions “{query}”.")
            return
        needle = find_needle(query)
        by_book: dict[int, list] = {}
        for hit in hits:
            by_book.setdefault(hit.book_id, []).append(hit)
        tomes = 0
        for book_id, book_hits in by_book.items():
            book = self.library.book(book_id)
            if book is None:
                continue
            tomes += 1
            count = len(book_hits)
            parent = QTreeWidgetItem(self.results)
            parent.setData(0, ROLE, (book_id, book_hits[0].page, needle))
            cover = QPixmap(str(self.library.cover_file(book_id)))
            if not cover.isNull():
                parent.setIcon(0, cover.scaledToHeight(
                    80, Qt.TransformationMode.SmoothTransformation))
            heading = QLabel(
                f'<span style="font-size:16px; color:{palette.SILVER_100}">'
                f"{html.escape(book.title)}</span>"
                f'&nbsp;&nbsp;<i style="color:{palette.SILVER_500}">'
                f"{count} page{'s' if count != 1 else ''}</i>")
            heading.setContentsMargins(4, 6, 4, 6)
            parent.setSizeHint(0, QSize(0, 58))
            self.results.setItemWidget(parent, 0, heading)
            for hit in sorted(book_hits, key=lambda h: h.page):
                child = QTreeWidgetItem(parent)
                child.setData(0, ROLE, (book_id, hit.page, needle))
                line = QLabel(
                    f'<span style="color:{palette.FROST_300}">p. {hit.page + 1}</span>'
                    f"&nbsp;&nbsp;{snippet_html(hit.snippet)}")
                line.setContentsMargins(4, 0, 12, 0)
                line.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                child.setSizeHint(0, QSize(0, 38))
                self.results.setItemWidget(child, 0, line)
            parent.setExpanded(True)
        shown = sum(len(v) for v in by_book.values())
        more = " (showing the best matches)" if len(hits) >= LIMIT else ""
        self.summary.setText(
            f"{shown} page{'s' if shown != 1 else ''} in {tomes} "
            f"tome{'s' if tomes != 1 else ''}{more}. Click a page to open it there.")

    def _activated(self, item, _column=0) -> None:
        data = item.data(0, ROLE)
        if data:
            self.openRequested.emit(*data)
