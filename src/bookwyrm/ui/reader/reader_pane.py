"""One reading surface: toolbar, find bar, side panel and a page view onto a session."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bookwyrm.db.repo import LibraryError
from bookwyrm.ui import prefs
from bookwyrm.ui.editor.scriptorium import Scriptorium
from bookwyrm.ui.extras.boards import image_bytes, thumbnail_bytes
from bookwyrm.ui.keys import hint, menu_text
from bookwyrm.ui.library import dialogs
from bookwyrm.ui.reader.find_bar import FindBar
from bookwyrm.ui.reader.page_view import READING_MODES, PageView
from bookwyrm.ui.reader.side_panels import SidePanel
from bookwyrm.ui.theme import icons, palette

ZOOM_PRESETS = (50, 75, 100, 125, 150, 200, 300, 400)
MODE_TIPS = {
    "day": "Daylight: pages as printed. Click for Candlelight.",
    "candle": "Candlelight: warm and dim for evening reading. Click for Night.",
    "night": "Night: light text on dark pages. Click for Daylight.",
}


class ReaderPane(QWidget):
    stateChanged = Signal()  # page or zoom changed; used to persist "resume here"
    notice = Signal(str)  # a short message for the status bar
    dirtyChanged = Signal(bool)  # unsaved edits appeared or were saved
    aboutToSave = Signal(int)  # book id: its file is about to be replaced
    tomeSaved = Signal(int)  # book id: its file changed on disk
    coverChanged = Signal(int)  # book id: its cover image was re-rendered

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.book_id: int | None = None  # set when the tome belongs to the library
        self.library = None
        self.tome_title = ""  # set by whoever opens the pane
        self._toc: list | None = None
        self.view = PageView(self)
        self.side = SidePanel(self)
        self.find = FindBar(self)
        self._search_id: int | None = None
        self._search_from: int | None = None  # start the next search at this page
        self._hit_counts: dict[int, int] = {}  # page -> matches on it
        self._searching = False
        self._clips: dict[int, tuple] = {}  # req_id -> (action, page, rect, extra)
        self._save_request: int | None = None
        self._origins: list | None = None  # current page -> page in the saved file
        self._was_dirty = False

        # ---- toolbar ----
        self._side_button = self._icon_button(
            "files", f"Show or hide the side panel ({hint('Ctrl+Alt+S')})",
            self.toggle_side, checkable=True)
        self._find_button = self._icon_button(
            "magnifying-glass", f"Find in this tome ({hint('Ctrl+F')})", self.open_find)
        self._ribbon_button = self._icon_button(
            "bookmarklet", f"Tie or untie a ribbon on this page ({hint('Ctrl+D')})",
            self.toggle_ribbon)
        self._mode_button = self._icon_button("candle-light", MODE_TIPS["day"],
                                              self.cycle_reading_mode)
        self._excerpt_button = self._icon_button(
            "scissors", f"Excerpt tool: drag a box to clip part of a page ({hint('Ctrl+E')})",
            self.toggle_excerpt_tool, checkable=True)
        self._pin_button = self._icon_button(
            "table", f"Pin this page to a Study Table ({hint('Ctrl+Shift+P')})",
            self.show_pin_menu)
        self._scriptorium_button = self._icon_button(
            "quill-ink", f"Scriptorium: annotate and edit this tome ({hint('Ctrl+Shift+E')})",
            self.toggle_scriptorium, checkable=True)
        self.scriptorium = Scriptorium(self, session, self.view)
        self.scriptorium.notice.connect(self.notice)
        self.scriptorium.stateChanged.connect(self._on_edit_state)

        self._zoom_out = self._text_button("−", f"Zoom out ({hint('Ctrl+-')})",
                                           self.view.zoom_out)
        self._zoom_in = self._text_button("+", f"Zoom in ({hint('Ctrl++')})",
                                          self.view.zoom_in)
        self._zoom_box = QComboBox(self)
        self._zoom_box.setEditable(True)
        self._zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._zoom_box.setToolTip("Zoom level")
        self._zoom_box.addItem("Fit Width", "fit_width")
        self._zoom_box.addItem("Fit Page", "fit_page")
        for percent in ZOOM_PRESETS:
            self._zoom_box.addItem(f"{percent}%", percent / 100)
        self._zoom_box.setMinimumWidth(112)
        self._zoom_box.activated.connect(self._zoom_chosen)
        self._zoom_box.lineEdit().editingFinished.connect(self._zoom_typed)

        self._prev = self._text_button("‹", "Previous page", lambda: self.view.step_page(-1))
        self._next = self._text_button("›", "Next page", lambda: self.view.step_page(1))
        self._labels: list | None = None  # the number printed on each page, if recorded
        self._page_field = QLineEdit(self)
        self._page_field.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._page_field.setFixedWidth(66)
        self._page_field.setToolTip("Go to page")
        self._page_field.returnPressed.connect(self._page_typed)
        self._page_total = QLabel("of –", self)
        self._back_button = self._text_button(
            "←", f"Back to where you were ({hint(QKeySequence.StandardKey.Back)})",
            self.view.go_back)
        self._forward_button = self._text_button(
            "→", f"Forward ({hint(QKeySequence.StandardKey.Forward)})", self.view.go_forward)
        self._back_button.setEnabled(False)
        self._forward_button.setEnabled(False)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        bar.setSpacing(4)
        bar.addWidget(self._side_button)
        bar.addSpacing(8)
        bar.addWidget(self._back_button)
        bar.addWidget(self._forward_button)
        bar.addSpacing(6)
        bar.addWidget(self._prev)
        bar.addWidget(self._page_field)
        bar.addWidget(self._page_total)
        bar.addWidget(self._next)
        bar.addStretch(1)
        bar.addWidget(self._find_button)
        bar.addWidget(self._ribbon_button)
        bar.addWidget(self._pin_button)
        bar.addWidget(self._excerpt_button)
        bar.addWidget(self._scriptorium_button)
        bar.addWidget(self._mode_button)
        bar.addSpacing(10)
        bar.addWidget(self._zoom_out)
        bar.addWidget(self._zoom_box)
        bar.addWidget(self._zoom_in)
        self.toolbar = QWidget(self)
        self.toolbar.setObjectName("readerToolbar")
        self.toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.toolbar.setLayout(bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.side)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([240, 900])
        self.splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.scriptorium.toolbar)
        layout.addWidget(self.find)
        layout.addWidget(self.splitter, 1)

        # ---- wiring ----
        self.view.loaded.connect(self._on_loaded)
        self.view.currentPageChanged.connect(self._on_page)
        self.view.zoomChanged.connect(self._on_zoom)
        self.side.outline.pageRequested.connect(self.view.jump_to)
        self.side.pages.pageRequested.connect(self.view.jump_to)
        self.side.ribbons.pageRequested.connect(self._jump_to_saved_page)
        self.view.linkActivated.connect(self.follow_link)
        self.view.historyChanged.connect(self._on_history)
        self.side.ribbons.addRequested.connect(self.toggle_ribbon)
        self.side.ribbons.renamed.connect(self._rename_ribbon)
        self.side.ribbons.removed.connect(self._remove_ribbon)
        self.side.ribbons.set_available(False)
        self.find.searchRequested.connect(self._start_search)
        self.find.nextRequested.connect(lambda: self.step_match(1))
        self.find.previousRequested.connect(lambda: self.step_match(-1))
        self.find.closed.connect(self._find_closed)
        session.found.connect(self._on_found)
        session.searchDone.connect(self._on_search_done)
        session.pixels.connect(self._on_clip)
        session.edited.connect(self._on_session_edited)
        session.saved.connect(self._on_saved)
        session.failed.connect(self._on_request_failed)
        self.view.excerptDrawn.connect(self._excerpt_menu)

        self.apply_preferences()
        self.view.set_session(session)

    def _text_button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    def _icon_button(self, icon: str, tip: str, slot, checkable: bool = False) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icons.icon(icon))
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.clicked.connect(lambda _checked=False: slot())
        return button

    # -- the Scriptorium: editing ------------------------------------------------

    def can_edit(self) -> bool:
        """Only library tomes are edited: Bookwyrm's copy, never a file it does not own."""
        return self.library is not None and self.book_id is not None

    def toggle_scriptorium(self) -> None:
        if not self.can_edit():
            self._scriptorium_button.setChecked(False)
            self.notice.emit("Add this PDF to the library to annotate or edit it. Bookwyrm "
                             "only changes its own copies.")
            return
        show = self.scriptorium.toolbar.isHidden()
        self.scriptorium.toolbar.setVisible(show)
        self._scriptorium_button.setChecked(show)
        if show and self.view.tool == "excerpt":
            self._excerpt_button.setChecked(False)
        if not show:
            self.scriptorium.leave()

    def _on_edit_state(self) -> None:
        dirty = self.session.dirty
        if dirty != self._was_dirty:
            self._was_dirty = dirty
            self.dirtyChanged.emit(dirty)

    def _on_session_edited(self, _request: int, state: dict, _result) -> None:
        origins = state.get("origins")
        count = state.get("page_count") or 0
        self._origins = None if origins == list(range(count)) else origins
        if state.get("toc") is not None and state["toc"] != self._toc:
            self._toc = state["toc"]
            self.side.set_toc(self._toc)
        if state.get("structural"):
            self._labels = state.get("labels")
            self.side.pages.set_session(self.session, state.get("sizes") or [], self._labels)
            self._on_loaded(toc=False)
        if self.library is not None:
            self._reload_ribbons()

    def _saved_page(self, page: int) -> int | None:
        """The number `page` has in the file on disk (None for a page not saved yet)."""
        if self._origins is None:
            return page
        return self._origins[page] if 0 <= page < len(self._origins) else None

    def save(self) -> bool:
        """Write unsaved edits to the library copy. Returns False if there was nothing to do."""
        if not self.can_edit() or not self.session.dirty or self._save_request is not None:
            return False
        self.view.commit_inline()
        self.notice.emit("Saving…")
        self.aboutToSave.emit(self.book_id)  # anything else reading the file should let go
        self._save_request = self.session.save(str(self.library.archive_file(self.book_id)))
        return True

    def _on_request_failed(self, request: int, message: str) -> None:
        if request == self._save_request:
            self._save_request = None
            self._on_edit_state()
            dialogs.complain(self, message)

    def _on_saved(self, request: int, report: dict) -> None:
        self._origins = None
        self._on_edit_state()
        if request != self._save_request:
            return  # another view of this tome asked; it does the bookkeeping
        self._save_request = None
        library, book_id = self.library, self.book_id
        if library is None:
            return
        library.update_book(book_id, file_hash=report["sha256"], file_size=report["file_size"],
                            page_count=report["page_count"], edited=1)
        if report.get("mapping") is not None:
            library.remap_pages(book_id, report["mapping"])
        self._reload_ribbons()
        self._clips[self.session.preview(0, 480, 0)] = ("cover", 0, None, {})
        self.tomeSaved.emit(book_id)
        if report.get("restored"):
            self.notice.emit("Restored. The tome is as it was when you first added it.")
        else:
            self.notice.emit("Saved. The original is kept in the archive and can be restored.")

    def restore_original(self) -> None:
        """Swap the archive copy back in (asked for, and confirmed, in the Scriptorium)."""
        if self.can_edit() and self._save_request is None:
            self.view.commit_inline(False)
            self.aboutToSave.emit(self.book_id)
            self._save_request = self.session.restore(
                str(self.library.archive_file(self.book_id)))

    # -- library link: ribbons ------------------------------------------------

    def attach_library(self, library, book_id: int) -> None:
        self.library = library
        self.book_id = book_id
        self.side.ribbons.set_available(True)
        library.subscribe(self._on_library_change)
        self._reload_ribbons()

    def detach_library(self) -> None:
        if self.library is not None:
            self.library.unsubscribe(self._on_library_change)
            self.library = None

    def _on_library_change(self, what: str) -> None:
        if what == "ribbons" and self.library is not None:
            self._reload_ribbons()

    def _reload_ribbons(self) -> None:
        ribbons = self.library.ribbons(self.book_id)
        now = (lambda saved: saved) if self._origins is None else \
            (lambda saved: self._origins.index(saved) if saved in self._origins else -1)
        printed = {r.page: self.page_text(now(r.page)) for r in ribbons if now(r.page) >= 0}
        self.side.ribbons.set_ribbons(ribbons, printed if self._labels else None)
        if self._origins is None:
            self.view.set_ribbon_pages(r.page for r in ribbons)
        else:  # pages have moved since the last save: show each ribbon where its page is now
            marked = {r.page for r in ribbons}
            self.view.set_ribbon_pages(
                now for now, saved in enumerate(self._origins) if saved in marked)

    def _section_title(self, page: int) -> str:
        """The contents entry that covers `page`, as a suggested ribbon name."""
        title = ""
        for _level, text, number, *_rest in (self.session.info or {}).get("toc", []):
            if number - 1 <= page:
                title = str(text).strip()
            else:
                break
        return title

    def toggle_ribbon(self) -> None:
        if self.library is None or not self.view.page_count():
            return
        current = self.view.current_page()
        page = self._saved_page(current)
        if page is None:
            self.notice.emit("Save the tome before tying a ribbon to a newly added page.")
            return
        here = [r for r in self.library.ribbons(self.book_id) if r.page == page]
        if here:
            for ribbon in here:
                self.library.delete_ribbon(ribbon.id)
        else:
            self.library.add_ribbon(self.book_id, page, self._section_title(current))

    def _rename_ribbon(self, ribbon_id: int, label: str) -> None:
        if self.library is not None:
            self.library.rename_ribbon(ribbon_id, label)

    def _remove_ribbon(self, ribbon_id: int) -> None:
        if self.library is not None:
            self.library.delete_ribbon(ribbon_id)

    # -- excerpts and pins -------------------------------------------------------

    def toggle_excerpt_tool(self) -> None:
        on = self.view.tool != "excerpt"
        if on:
            self.scriptorium.leave()
        self.view.set_tool("excerpt" if on else "select")
        self._excerpt_button.setChecked(on)
        if on:
            self.notice.emit("Excerpt tool: drag a box around what you want to keep.")

    def _tables_menu(self, menu: QMenu, handler) -> None:
        """Fill `menu` with the Study Tables; `handler(table_id)` runs on a choice."""
        tables = self.library.tables()
        for table in tables:
            menu.addAction(menu_text(table.name), lambda t=table.id: handler(t))
        if tables:
            menu.addSeparator()
        menu.addAction("New Study Table…", lambda: self._new_table(handler))

    def _new_table(self, handler) -> None:
        name = dialogs.ask_name(self, "New Study Table", "Name for the new study table:")
        if name is None:
            return
        try:
            handler(self.library.add_table(name).id)
        except LibraryError as problem:
            dialogs.complain(self, str(problem))

    def show_pin_menu(self) -> None:
        if self.library is None:
            self.notice.emit("Add this PDF to the library to pin its pages to a Study Table.")
            return
        if not self.view.page_count():
            return
        menu = QMenu(self)
        self._tables_menu(menu, self._pin_page)
        menu.exec(self._pin_button.mapToGlobal(QPoint(0, self._pin_button.height())))

    def _pin_page(self, table_id: int) -> None:
        page = self.view.current_page()
        request = self.session.preview(page, 420, 0)
        self._clips[request] = ("pin", page, None, {"table": table_id})

    def _excerpt_menu(self, page: int, rect: QRectF, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Copy Image", lambda: self._clip("copy", page, rect))
        menu.addAction("Save as PNG…", lambda: self._clip("save", page, rect))
        if self.library is not None:
            menu.addAction("Add to Commonplace Book", lambda: self._clip("keep", page, rect))
            pins = menu.addMenu("Pin to Study Table")
            self._tables_menu(pins, lambda table_id: self._clip("pin", page, rect,
                                                                table=table_id))
        menu.exec(pos)
        if not any(entry[1] == page for entry in self._clips.values()):
            self.view.clear_marquee()

    def _clip(self, action: str, page: int, rect: QRectF, **extra) -> None:
        """Ask the host for a sharp picture of `rect`; `_on_clip` finishes the job."""
        longest = max(rect.width(), rect.height(), 1.0)
        scale = min(4.0, 3600.0 / longest)  # about 290 dpi, capped for very large regions
        box = (rect.left(), rect.top(), rect.right(), rect.bottom())
        request = self.session.tile(page, scale, box, 0)
        self._clips[request] = (action, page, box, extra)

    def _on_clip(self, request: int, image) -> None:
        entry = self._clips.pop(request, None)
        if entry is None:
            return
        action, page, box, extra = entry
        label = self._section_title(page)
        if action == "cover":
            if self.library is not None:
                image.save(str(self.library.cover_file(self.book_id)), "JPG", 86)
                self.coverChanged.emit(self.book_id)
            return
        if action in ("keep", "pin"):
            saved_page = self._saved_page(page)
            if saved_page is None:
                self.notice.emit("Save the tome before keeping part of a newly added page.")
                self.view.clear_marquee()
                return
            page = saved_page
        if action == "copy":
            QApplication.clipboard().setImage(image)
            self.notice.emit("Excerpt copied to the clipboard.")
        elif action == "save":
            path, _ = QFileDialog.getSaveFileName(
                self, "Save excerpt", f"excerpt-p{page + 1}.png", "PNG image (*.png)")
            if path:
                image.save(path, "PNG")
                self.notice.emit(f"Excerpt saved to {path}.")
        elif action == "keep" and self.library is not None:
            self.library.add_excerpt(image_bytes(image), self.book_id, page, box, label)
            self.notice.emit("Excerpt added to the Commonplace Book.")
        elif action == "pin" and self.library is not None:
            table = self.library.table(extra["table"])
            if table is not None:
                self.library.add_pin(table.id, self.book_id, page, box, label,
                                     thumbnail_bytes(image))
                self.notice.emit(f"Pinned page {page + 1} to “{table.name}”.")
        self.view.clear_marquee()

    def go_to_region(self, page: int, rect=None) -> None:
        """Jump to a pinned or excerpted spot and outline it."""
        def act():
            if rect is None:
                self.view.jump_to(page)
                return
            box = QRectF(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])
            self.view.jump_to(page, max(0.0, box.top() - 24))
            self.view.marquee = (page, box)
            self.view.viewport().update()
        if self.view.page_count():
            act()
        else:
            self.view.loaded.connect(act, Qt.ConnectionType.SingleShotConnection)

    # -- side panel and reading mode ---------------------------------------------

    def apply_preferences(self) -> None:
        """Take up the reading preferences (when opened, and when Settings change them)."""
        show_side = prefs.get("reader/side_panel")
        self.side.setVisible(show_side)
        self._side_button.setChecked(show_side)
        self.set_reading_mode(prefs.get("reader/mode"))
        self.view.set_spread(prefs.get("reader/spread"))

    # -- getting around: printed page numbers, Back and Forward, links -------------

    def page_text(self, index: int) -> str:
        """What to call a page: the number printed on it if the tome records one."""
        if self._labels and 0 <= index < len(self._labels):
            return self._labels[index]
        return str(index + 1)

    def page_for(self, text: str) -> int | None:
        """The page a typed number means, or None.

        A printed page number wins ("86" is the page printed 86, wherever that falls
        in the file). "#93" always means the 93rd page of the file.
        """
        text = text.strip()
        count = self.view.page_count()
        if text.startswith("#"):
            text, by_label = text[1:].strip(), False
        else:
            by_label = bool(self._labels)
        if by_label:
            wanted = text.casefold()
            for index, label in enumerate(self._labels):
                if label.casefold() == wanted:
                    return index
        try:
            number = int(text)
        except ValueError:
            return None
        return number - 1 if 1 <= number <= count else None

    def _page_typed(self) -> None:
        page = self.page_for(self._page_field.text())
        if page is None:
            self.notice.emit(f"This tome has no page “{self._page_field.text().strip()}”.")
            self._on_page(self.view.current_page())
        else:
            self.view.jump_to(page)
        self.view.setFocus()

    def _on_history(self, back: bool, forward: bool) -> None:
        self._back_button.setEnabled(back)
        self._forward_button.setEnabled(forward)

    def _jump_to_saved_page(self, page: int) -> None:
        """Go to a page numbered as in the saved file (ribbons are stored that way)."""
        if self._origins is not None and page in self._origins:
            page = self._origins.index(page)
        self.view.jump_to(page)

    def follow_link(self, link: dict) -> None:
        """A link in the tome was clicked: jump there, or offer to open a web address."""
        if "page" in link:
            self.view.jump_to(link["page"], max(0.0, float(link.get("y", 0.0)) - 18.0))
            return
        address = QUrl(link.get("uri", ""))
        if address.scheme().lower() not in ("http", "https", "mailto"):
            self.notice.emit("That link leads outside the tome and cannot be opened here.")
            return
        if dialogs.confirm(self, "Open Link",
                           f"Open this address outside Bookwyrm?\n\n{address.toString()}",
                           "Open"):
            QDesktopServices.openUrl(address)

    def toggle_spread(self) -> None:
        prefs.put("reader/spread", not self.view.spread)
        self.view.set_spread(prefs.get("reader/spread"))

    def print_tome(self) -> None:
        from bookwyrm.ui.reader.printing import print_tome
        print_tome(self, self.tome_title or "this tome")

    def set_chrome(self, visible: bool) -> None:
        """Hide the toolbars and side panel for full-screen reading, or bring them back."""
        if not visible:
            self._chrome = (self.side.isVisible(), self.scriptorium.toolbar.isVisible())
            for widget in (self.toolbar, self.side, self.scriptorium.toolbar):
                widget.hide()
        else:
            side, tools = getattr(self, "_chrome", (prefs.get("reader/side_panel"), False))
            self.toolbar.show()
            self.side.setVisible(side)
            self.scriptorium.toolbar.setVisible(tools)

    def toggle_side(self) -> None:
        show = self.side.isHidden()
        self.side.setVisible(show)
        self._side_button.setChecked(show)
        prefs.put("reader/side_panel", show)

    def set_reading_mode(self, mode: str) -> None:
        if mode not in READING_MODES:
            mode = "day"
        self.view.set_reading_mode(mode)
        self._mode_button.setToolTip(MODE_TIPS[mode])
        color = {"day": palette.SILVER_300, "candle": palette.AMBER_400,
                 "night": palette.FROST_500}[mode]
        self._mode_button.setIcon(icons.icon("candle-light", color, color))

    def cycle_reading_mode(self) -> None:
        current = READING_MODES.index(self.view.reading_mode)
        mode = READING_MODES[(current + 1) % len(READING_MODES)]
        self.set_reading_mode(mode)
        prefs.put("reader/mode", mode)

    # -- copy ----------------------------------------------------------------------

    def copy_selection(self) -> bool:
        text = self.view.selected_text()
        if text:
            QApplication.clipboard().setText(text)
        return bool(text)

    # -- find in tome ------------------------------------------------------------------

    def open_find(self, text: str | None = None) -> None:
        if text is None and self.view.has_selection():
            chosen = self.view.selected_text()
            if "\n" not in chosen and len(chosen) <= 80:
                text = chosen
        self.find.open_bar(text)

    def _start_search(self, needle: str) -> None:
        self.view.clear_hits()
        self._hit_counts = {}
        if len(needle) < 2:
            self.session.stop_search()
            self._search_id = None
            self._searching = False
            self.find.set_status("")
            return
        self._searching = True
        self.find.set_status("Searching…")
        start = self.view.current_page() if self._search_from is None else self._search_from
        self._search_from = None
        self._search_id = self.session.search(needle, start)

    def _on_found(self, search_id: int, page: int, rects: list) -> None:
        if search_id != self._search_id:
            return
        first = not self._hit_counts
        self._hit_counts[page] = len(rects)
        self.view.add_hits(page, rects)
        if first:  # the search starts at the current page, so this is the nearest match
            self.view.set_current_hit(page, 0)
        self._update_find_status()

    def _on_search_done(self, search_id: int, total: int) -> None:
        if search_id != self._search_id:
            return
        self._searching = False
        self._update_find_status()

    def _matches(self) -> list[tuple[int, int]]:
        return [(page, index) for page in sorted(self._hit_counts)
                for index in range(self._hit_counts[page])]

    def _update_find_status(self) -> None:
        matches = self._matches()
        if not matches:
            self.find.set_status("Searching…" if self._searching else "No matches")
            return
        current = self.view.current_hit
        position = matches.index(current) + 1 if current in matches else 0
        more = "+" if self._searching else ""
        self.find.set_status(f"{position} of {len(matches)}{more}")

    def step_match(self, direction: int) -> None:
        matches = self._matches()
        if not matches:
            return
        current = self.view.current_hit
        index = matches.index(current) if current in matches else -1
        page, number = matches[(index + direction) % len(matches)]
        self.view.set_current_hit(page, number)
        self._update_find_status()

    def _find_closed(self) -> None:
        self.session.stop_search()
        self._search_id = None
        self._searching = False
        self._hit_counts = {}
        self.view.clear_hits()
        self.view.setFocus()

    # -- state --------------------------------------------------------------

    def state(self) -> dict:
        return {
            "page": self.view.current_page(),
            "offset": self.view.page_offset(),
            "zoom": self.view.zoom(),
            "mode": self.view.zoom_mode(),
        }

    def restore(self, state: dict | None) -> None:
        """Apply a saved state now, or as soon as the tome has loaded."""
        if not state:
            return
        if self.view.page_count():
            self._apply_state(state)
        else:
            self.view.loaded.connect(lambda: self._apply_state(state),
                                     Qt.ConnectionType.SingleShotConnection)

    def _apply_state(self, state: dict) -> None:
        mode = state.get("mode", "fit_width")
        if mode == "fit_width":
            self.view.fit_width()
        elif mode == "fit_page":
            self.view.fit_page()
        else:
            self.view.set_zoom(float(state.get("zoom", 1.0)))
        self.view.go_to_page(int(state.get("page", 0)), float(state.get("offset", 0.0)))

    def go_to(self, page: int, find: str | None = None) -> None:
        """Jump to a page (once loaded) and optionally search for `find` there."""
        def act():
            self.view.jump_to(page)
            if find:
                self._search_from = page  # the view has not caught up with the jump yet
                self.find.field.clear()
                self.open_find(find)
        if self.view.page_count():
            act()
        else:
            self.view.loaded.connect(act, Qt.ConnectionType.SingleShotConnection)

    # -- slots --------------------------------------------------------------

    def _on_loaded(self, toc: bool = True) -> None:
        info = self.session.info or {}
        if toc:
            self._labels = info.get("labels")
            self._toc = info.get("toc", [])
            self.side.set_toc(self._toc)
            self.side.pages.set_session(self.session, info.get("sizes", []), self._labels)
        self._page_field.setToolTip(
            "Go to a page by the number printed on it. Put # first to count pages from the "
            "start of the file instead, as in #93." if self._labels else "Go to page")
        self._on_page(self.view.current_page(), announce=False)
        if self.library is not None:
            self._reload_ribbons()

    def _on_page(self, index: int, announce: bool = True) -> None:
        count = self.view.page_count()
        if not self._page_field.hasFocus():
            self._page_field.setText(self.page_text(index))
        self._page_total.setText(f"({index + 1} of {count})" if self._labels
                                 else f"of {count}")
        self.side.outline.follow_page(index)
        self.side.pages.follow_page(index)
        if announce:
            self.stateChanged.emit()

    def _on_zoom(self, factor: float, mode: str) -> None:
        self._zoom_box.blockSignals(True)
        self._zoom_box.setEditText(f"{round(factor * 100)}%")
        self._zoom_box.blockSignals(False)
        self.stateChanged.emit()

    def _zoom_chosen(self, row: int) -> None:
        value = self._zoom_box.itemData(row)
        if value == "fit_width":
            self.view.fit_width()
        elif value == "fit_page":
            self.view.fit_page()
        elif value:
            self.view.set_zoom(float(value))
        self.view.setFocus()

    def _zoom_typed(self) -> None:
        text = self._zoom_box.currentText().strip().rstrip("%").strip()
        try:
            percent = float(text)
        except ValueError:
            self._on_zoom(self.view.zoom(), self.view.zoom_mode())
            return
        if abs(percent / 100 - self.view.zoom()) > 0.004:
            self.view.set_zoom(percent / 100)
