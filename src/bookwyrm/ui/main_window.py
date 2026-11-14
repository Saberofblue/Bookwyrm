"""Main window: the Library tab plus one tab per open tome, in one or two side-by-side groups."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import QEventLoop, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabBar,
    QTabWidget,
    QTextEdit,
)

from bookwyrm.db.repo import Library
from bookwyrm.library.importer import Importer
from bookwyrm.pdf.session_client import DocumentSession
from bookwyrm.ui import prefs
from bookwyrm.ui.library.library_page import LibraryPage
from bookwyrm.ui.reader.reader_pane import ReaderPane
from bookwyrm.ui.theme import icons


def _tab_title(title: str) -> str:
    return title if len(title) <= 28 else title[:27].rstrip() + "…"


class SessionPool:
    """Hands out one shared DocumentSession per file, closing it when the last view goes."""

    def __init__(self, owner):
        self._owner = owner
        self._sessions: dict[str, list] = {}  # path -> [session, views]

    def acquire(self, path: str) -> DocumentSession:
        entry = self._sessions.get(path)
        if entry is None:
            entry = self._sessions[path] = [DocumentSession(path, self._owner), 0]
        entry[1] += 1
        return entry[0]

    def release(self, session: DocumentSession) -> None:
        entry = self._sessions.get(session.path)
        if entry is None or entry[0] is not session:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            del self._sessions[session.path]
            session.close()
            session.deleteLater()


class MainWindow(QMainWindow):
    libraryChangeRequested = Signal(str, str)  # folder, "move" | "switch"

    def __init__(self, library: Library):
        super().__init__()
        self.library = library
        self._released = False  # True once the library has been let go of (closing or moving)
        self.settings = QSettings()
        self.importer = Importer(library, self)
        self.sessions = SessionPool(self)
        self.setWindowTitle("Bookwyrm")
        self.resize(1280, 860)

        self.library_page = LibraryPage(library, self.importer, self)
        self.left = self._make_group()
        self.right = self._make_group()
        self._active = self.left  # the group that menus and shortcuts act on
        self.tabs = self.left  # the main group; it always holds the Library
        self.left.addTab(self.library_page, icons.icon("bookshelf"), "Library")
        for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
            self.left.tabBar().setTabButton(0, side, None)  # the Library never closes
        self.split = QSplitter(Qt.Orientation.Horizontal, self)
        self.split.addWidget(self.left)
        self.split.addWidget(self.right)
        self.split.setChildrenCollapsible(False)
        self.right.hide()
        self.setCentralWidget(self.split)
        QApplication.instance().focusChanged.connect(self._on_focus)

        self._status = QLabel("")
        self.statusBar().addWidget(self._status, 1)
        self._notice_timer = QTimer(self, singleShot=True, interval=6000)
        self._notice_timer.timeout.connect(lambda: self._show_status(self._librarian))
        self._librarian = ""  # ongoing work, shown whenever no brief notice is up
        self.importer.status.connect(self._on_librarian)
        self.library_page.message.connect(self.notice)
        self.library_page.openBook.connect(self.open_book)
        self.library_page.openBookAt.connect(self.open_book_at)
        self.library_page.openRegion.connect(self.open_book_region)
        self.library_page.aboutToRemove.connect(self._close_book)
        self.library_page.settingsRequested.connect(self.show_settings)
        self.library_page.unsaved = lambda book_id: any(
            pane.book_id == book_id and pane.session.dirty for pane in self._panes())

        self._dirty: set[ReaderPane] = set()
        self._save_timer = QTimer(self, singleShot=True, interval=800)
        self._save_timer.timeout.connect(self._save_reading_states)
        self._build_menus()

        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        QTimer.singleShot(0, self._tidy_library)

    def _tidy_library(self) -> None:
        """Finish or clear away whatever an earlier session left half-done."""
        if self._released:
            return
        self.importer.resume()
        self.library.sweep()
        self.restore_session()

    # -- last session's tomes -------------------------------------------------------

    def _save_session(self) -> None:
        tomes = [{"book": pane.book_id, "side": 0 if self._group_of(pane) is self.left else 1}
                 for pane in self._panes() if pane.book_id is not None]
        current = self.current_pane()
        self.settings.setValue("session/tomes", json.dumps(tomes))
        self.settings.setValue("session/active",
                               current.book_id if current is not None else None)
        self.settings.setValue("session/library", str(self.library.root))

    def restore_session(self) -> None:
        """Reopen the tomes that were open when Bookwyrm last closed (a preference)."""
        if not prefs.get("startup/reopen") or self._panes():
            return
        if self.settings.value("session/library") != str(self.library.root):
            return
        try:
            tomes = json.loads(self.settings.value("session/tomes") or "[]")
            active = self.settings.value("session/active")
            active = int(active) if active not in (None, "", "None") else None
        except (TypeError, ValueError):
            return
        for entry in tomes:
            book = self.library.book(entry.get("book"))
            if book is not None and book.ready and self.library.book_file(book).exists():
                self.open_book(book.id, group=self.right if entry.get("side") else self.left)
        for pane in self._panes():
            if pane.book_id == active:
                self._focus_pane(pane)
                return
        if tomes and active is None:
            self.show_library()

    def _make_group(self) -> QTabWidget:
        group = QTabWidget(self)
        group.setDocumentMode(True)
        group.setTabsClosable(True)
        group.setMovable(False)
        group.tabCloseRequested.connect(lambda index, g=group: self._close_index(g, index))
        group.currentChanged.connect(lambda _index, g=group: self._on_tab_changed(g))
        return group

    # -- menus ----------------------------------------------------------------

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._add(file_menu, "&Add Tomes…", QKeySequence.StandardKey.Open,
                  lambda: (self.show_library(), self.library_page.add_tomes()))
        self._add(file_menu, "New &Bookcase…", QKeySequence("Ctrl+Shift+N"),
                  lambda: (self.show_library(), self.library_page.create_place("bookcase")))
        self._add(file_menu, "New Study &Table…", None,
                  lambda: (self.show_library(), self.library_page.create_table()))
        file_menu.addSeparator()
        settings = self._add(file_menu, "Se&ttings…", QKeySequence("Ctrl+,"), self.show_settings)
        settings.setMenuRole(QAction.MenuRole.PreferencesRole)  # a Mac puts it in the app menu
        file_menu.addSeparator()
        self._add(file_menu, "&Save Changes", QKeySequence.StandardKey.Save, self._pane("save"))
        self._add(file_menu, "&Close Tome", QKeySequence.StandardKey.Close, self.close_current)

        edit = self.menuBar().addMenu("&Edit")
        self._add(edit, "&Undo", QKeySequence.StandardKey.Undo, lambda: self._history("undo"))
        self._add(edit, "&Redo", QKeySequence.StandardKey.Redo, lambda: self._history("redo"))
        edit.addSeparator()
        self._add(edit, "&Copy", QKeySequence.StandardKey.Copy, self._copy)
        self._add(edit, "Select &All on Page", QKeySequence.StandardKey.SelectAll,
                  self._view("select_all_on_page"))
        edit.addSeparator()
        self._add(edit, "&Find in Tome…", QKeySequence.StandardKey.Find, self._pane("open_find"))
        self._add(edit, "Find &Next", QKeySequence.StandardKey.FindNext,
                  self._pane("step_match", 1))
        self._add(edit, "Find &Previous", QKeySequence.StandardKey.FindPrevious,
                  self._pane("step_match", -1))
        self._add(edit, "Search the Card Catalo&gue…", QKeySequence("Ctrl+Shift+F"),
                  self.show_catalogue)
        edit.addSeparator()
        self._add(edit, "&Excerpt Tool", QKeySequence("Ctrl+E"),
                  self._pane("toggle_excerpt_tool"))
        self._add(edit, "Pin Page to a Study &Table…", QKeySequence("Ctrl+Shift+P"),
                  self._pane("show_pin_menu"))
        edit.addSeparator()
        self._add(edit, "&Scriptorium (Annotate and Edit)", QKeySequence("Ctrl+Shift+E"),
                  self._pane("toggle_scriptorium"))

        view = self.menuBar().addMenu("&View")
        self._add(view, "Zoom &In", QKeySequence.StandardKey.ZoomIn, self._view("zoom_in"))
        self._add(view, "Zoom &Out", QKeySequence.StandardKey.ZoomOut, self._view("zoom_out"))
        self._add(view, "&Actual Size", QKeySequence("Ctrl+0"), self._view("actual_size"))
        self._add(view, "Fit &Width", QKeySequence("Ctrl+1"), self._view("fit_width"))
        self._add(view, "Fit &Page", QKeySequence("Ctrl+2"), self._view("fit_page"))
        view.addSeparator()
        self._add(view, "&Side Panel", QKeySequence("Ctrl+Alt+S"), self._pane("toggle_side"))
        self._add(view, "Next &Reading Mode", QKeySequence("Ctrl+Shift+L"),
                  self._pane("cycle_reading_mode"))
        view.addSeparator()
        self._add(view, "&Move Tome to the Other Side", QKeySequence("Ctrl+\\"),
                  self.move_to_other_side)
        self._add(view, "Open This Tome &Alongside", QKeySequence("Ctrl+Shift+\\"),
                  self.open_alongside)

        go = self.menuBar().addMenu("&Go")
        self._add(go, "&Library", QKeySequence("Ctrl+L"), self.show_library)
        self._add(go, "Tie or Untie a &Ribbon", QKeySequence("Ctrl+D"),
                  self._pane("toggle_ribbon"))
        go.addSeparator()
        self._add(go, "&Next Page", QKeySequence("Ctrl+Right"), self._view("step_page", 1))
        self._add(go, "&Previous Page", QKeySequence("Ctrl+Left"), self._view("step_page", -1))
        self._add(go, "&First Page", QKeySequence("Ctrl+Up"), self._view("go_to_page", 0))
        self._add(go, "Las&t Page", QKeySequence("Ctrl+Down"), self._view("go_to_page", 10**9))

        help_menu = self.menuBar().addMenu("&Help")
        self._add(help_menu, "About Bookwyrm and Credits", None, self.show_about)

    def _add(self, menu, text, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        return action

    def _view(self, method: str, *args):
        def call():
            pane = self.current_pane()
            if pane is not None:
                getattr(pane.view, method)(*args)
        return call

    def _pane(self, method: str, *args):
        def call():
            pane = self.current_pane()
            if pane is not None:
                getattr(pane, method)(*args)
        return call

    def _history(self, direction: str) -> None:
        """Undo or redo: in a text field if one has focus, otherwise in the open tome."""
        focus = self.focusWidget()
        if isinstance(focus, QLineEdit | QPlainTextEdit | QTextEdit):
            getattr(focus, direction)()
            return
        pane = self.current_pane()
        if pane is not None:
            getattr(pane.scriptorium, direction)()

    def _copy(self) -> None:
        pane = self.current_pane()
        focus = self.focusWidget()
        if pane is not None and pane.view.has_selection() and not hasattr(focus, "copy"):
            pane.copy_selection()
        elif hasattr(focus, "copy"):
            focus.copy()

    def show_settings(self) -> None:
        from bookwyrm.ui.settings import SettingsDialog

        dialog = SettingsDialog(self.library, self)
        dialog.coverHeightChanged.connect(self.library_page.set_cover_height)
        dialog.readingChanged.connect(self._apply_reading_preferences)
        dialog.libraryChangeRequested.connect(self.libraryChangeRequested)
        dialog.exec()

    def _apply_reading_preferences(self) -> None:
        for pane in self._panes():
            pane.apply_preferences()

    def show_about(self) -> None:
        from bookwyrm import __version__
        from bookwyrm.ui.library.dialogs import AboutDialog
        AboutDialog(__version__, self).exec()

    def show_catalogue(self) -> None:
        self.show_library()
        self.library_page.show_catalogue()

    # -- status bar -------------------------------------------------------------

    def _show_status(self, text: str) -> None:
        self._status.setText(text)

    def _on_librarian(self, text: str) -> None:
        self._librarian = text
        if not self._notice_timer.isActive():
            self._show_status(text)

    def notice(self, text: str) -> None:
        """A brief message that gives way to the Librarian's ongoing status."""
        self._show_status(text)
        self._notice_timer.start()

    # -- tab groups -----------------------------------------------------------------

    def _groups(self) -> tuple[QTabWidget, QTabWidget]:
        return self.left, self.right

    def _other(self, group: QTabWidget) -> QTabWidget:
        return self.right if group is self.left else self.left

    def _group_of(self, widget) -> QTabWidget | None:
        for group in self._groups():
            if group.indexOf(widget) >= 0:
                return group
        return None

    def _on_focus(self, _old, new) -> None:
        widget = new
        while widget is not None:
            if widget in self._groups():
                if widget is not self._active:
                    self._active = widget
                    self._update_title()
                return
            widget = widget.parentWidget()

    def current_pane(self) -> ReaderPane | None:
        widget = self._active.currentWidget()
        return widget if isinstance(widget, ReaderPane) else None

    def _panes(self) -> list[ReaderPane]:
        return [group.widget(i) for group in self._groups() for i in range(group.count())
                if isinstance(group.widget(i), ReaderPane)]

    def show_library(self) -> None:
        self.left.setCurrentIndex(0)
        self._active = self.left
        self._update_title()

    def _update_title(self) -> None:
        pane = self.current_pane()
        if pane is None:
            self.setWindowTitle("Bookwyrm")
        else:
            group = self._group_of(pane)
            self.setWindowTitle(f"{group.tabToolTip(group.indexOf(pane))} — Bookwyrm")

    def _on_tab_changed(self, group: QTabWidget) -> None:
        if group is self._active:
            self._update_title()
        widget = group.currentWidget()
        if isinstance(widget, ReaderPane):
            widget.view.setFocus()

    # -- opening --------------------------------------------------------------------

    def open_book(self, book_id: int, group: QTabWidget | None = None,
                  duplicate: bool = False) -> ReaderPane | None:
        if not duplicate:
            for pane in self._panes():
                if pane.book_id == book_id:
                    self._focus_pane(pane)
                    return pane
        book = self.library.book(book_id)
        if book is None:
            return None
        path = self.library.book_file(book)
        if not path.exists():
            QMessageBox.warning(
                self, "Bookwyrm",
                f"“{book.title}” is missing from the library folder.\n\nExpected at:\n{path}")
            return None
        pane = self._open_pane(str(path), book.title, group or self.left)
        pane.attach_library(self.library, book_id)
        if book.last_opened_at is None:  # never read before: start the way Settings say
            first = prefs.get("reader/zoom")
            pane.restore({"page": 0, "offset": 0.0, "zoom": 1.0,
                          "mode": "custom" if first == "actual" else first})
        else:
            pane.restore({"page": book.last_page, "offset": book.last_offset,
                          "zoom": book.last_zoom, "mode": book.last_zoom_mode})
        self.library.mark_opened(book_id)
        return pane

    def open_book_at(self, book_id: int, page: int, find: str = "") -> None:
        """Open a tome at a page, highlighting `find` there (from the Card Catalogue)."""
        pane = self.open_book(book_id)
        if pane is not None:
            pane.go_to(page, find or None)

    def open_book_region(self, book_id: int, page: int, rect=None) -> None:
        """Open a tome at a pinned or excerpted spot."""
        pane = self.open_book(book_id)
        if pane is not None:
            pane.go_to_region(page, rect)

    def open_path(self, path: str) -> ReaderPane:
        """Open a PDF that is not in the library (nothing about it is remembered)."""
        return self._open_pane(path, Path(path).stem, self.left)

    def _open_pane(self, path: str, title: str, group: QTabWidget) -> ReaderPane:
        session = self.sessions.acquire(path)
        pane = ReaderPane(session, self)
        pane.tome_title = title
        session.crashed.connect(lambda message, p=pane: self._on_crashed(p, message))
        session.password_required.connect(lambda p=pane: self._ask_password(p))
        pane.stateChanged.connect(lambda p=pane: self._mark_dirty(p))
        pane.notice.connect(self.notice)
        pane.dirtyChanged.connect(lambda _dirty, s=session: self._mark_unsaved(s))
        pane.aboutToSave.connect(self.importer.forget)  # stop reading a file about to change
        pane.tomeSaved.connect(self.importer.reindex)
        pane.coverChanged.connect(self.library_page.view.invalidate_cover)
        pane.scriptorium.extracted.connect(self._import_extract)
        pane.scriptorium.retitled.connect(lambda new, s=session: self._retitle(s, new))
        index = group.addTab(pane, icons.icon("open-book"), _tab_title(title))
        group.setTabToolTip(index, title)
        self._mark_unsaved(session)
        self._focus_pane(pane)
        return pane

    def _focus_pane(self, pane: ReaderPane) -> None:
        group = self._group_of(pane)
        if group is None:
            return
        group.show()
        group.setCurrentWidget(pane)
        self._active = group
        pane.view.setFocus()
        self._update_title()

    # -- side by side -----------------------------------------------------------------

    def move_to_other_side(self) -> None:
        pane = self.current_pane()
        if pane is None:
            return
        source = self._group_of(pane)
        target = self._other(source)
        index = source.indexOf(pane)
        title, tip = source.tabText(index), source.tabToolTip(index)
        source.removeTab(index)
        new_index = target.addTab(pane, icons.icon("open-book"), title)
        target.setTabToolTip(new_index, tip)
        self._tidy_groups()
        self._focus_pane(pane)

    def open_alongside(self) -> None:
        """Show the current tome in the other group too, e.g. rules beside a stat block."""
        pane = self.current_pane()
        if pane is None:
            return
        source = self._group_of(pane)
        target = self._other(source)
        index = source.indexOf(pane)
        state = pane.state()
        if pane.book_id is not None:
            twin = self.open_book(pane.book_id, group=target, duplicate=True)
        else:
            twin = self._open_pane(pane.session.path, source.tabToolTip(index), target)
        if twin is not None:
            twin.restore(state)
            self._equalise()

    def _tidy_groups(self) -> None:
        was_hidden = self.right.isHidden()
        self.right.setVisible(self.right.count() > 0)
        if was_hidden and not self.right.isHidden():
            self._equalise()
        if self.right.isHidden() and self._active is self.right:
            self._active = self.left

    def _equalise(self) -> None:
        if not self.right.isHidden():
            half = max(1, self.split.width() // 2)
            self.split.setSizes([half, half])

    # -- closing ----------------------------------------------------------------------

    def _close_book(self, book_id: int) -> None:
        """Close every view of a tome that is about to be removed from the library.

        Its file must not be open when it is deleted (Windows would refuse).
        """
        for pane in self._panes():
            if pane.book_id == book_id:
                self.close_pane(pane, ask=False)

    def close_current(self) -> None:
        pane = self.current_pane()
        if pane is not None:
            self.close_pane(pane)

    def close_tab(self, index: int) -> None:
        """Close a tab of the main group by position."""
        self._close_index(self.left, index)

    def _close_index(self, group: QTabWidget, index: int) -> None:
        pane = group.widget(index)
        if isinstance(pane, ReaderPane):
            self.close_pane(pane)

    def _import_extract(self, path: str) -> None:
        """Pages copied out of a tome arrive as a temporary PDF: add it, then tidy up."""
        def tidy(_summary) -> None:
            self.importer.batchFinished.disconnect(tidy)
            shutil.rmtree(Path(path).parent, ignore_errors=True)
        self.importer.batchFinished.connect(tidy)
        self.importer.import_files([path], None)

    def _retitle(self, session, title: str) -> None:
        for pane in self._panes():
            if pane.session is session:
                pane.tome_title = title
                group = self._group_of(pane)
                group.setTabToolTip(group.indexOf(pane), title)
        self._mark_unsaved(session)
        self._update_title()

    def _mark_unsaved(self, session) -> None:
        """Show a dot on the tab of every view of a tome with unsaved edits."""
        for pane in self._panes():
            if pane.session is session:
                group = self._group_of(pane)
                index = group.indexOf(pane)
                title = _tab_title(group.tabToolTip(index))
                group.setTabText(index, f"• {title}" if session.dirty else title)

    def _ask_unsaved(self, title: str) -> str:
        """'save', 'discard' or 'cancel' for a tome with unsaved edits."""
        box = QMessageBox(QMessageBox.Icon.Question, "Unsaved Changes",
                          f"“{title}” has changes that are not saved.", parent=self)
        box.setInformativeText("Save them to your library copy before closing?")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard Changes", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        return "save" if clicked is save else "discard" if clicked is discard else "cancel"

    def _save_and_wait(self, pane: ReaderPane) -> bool:
        """Save a tome and wait for the helper to finish (it takes well under a second)."""
        if not pane.save():
            return not pane.session.dirty
        loop = QEventLoop(self)
        pane.session.saved.connect(loop.quit)
        pane.session.failed.connect(loop.quit)
        pane.session.crashed.connect(loop.quit)
        QTimer.singleShot(30000, loop.quit)
        loop.exec()
        return not pane.session.dirty

    def _may_close(self, pane: ReaderPane) -> bool:
        """Settle unsaved edits before the last view of a tome goes away."""
        session = pane.session
        if not session.dirty or any(p is not pane and p.session is session
                                    for p in self._panes()):
            return True
        group = self._group_of(pane)
        answer = self._ask_unsaved(group.tabToolTip(group.indexOf(pane)))
        if answer == "save":
            return self._save_and_wait(pane)
        return answer == "discard"

    def close_pane(self, pane: ReaderPane, ask: bool = True) -> None:
        group = self._group_of(pane)
        if group is None:
            return
        if ask and not self._may_close(pane):
            return
        self._save_pane(pane)
        pane.blockSignals(True)  # a closing tome must not ask to be remembered again
        self._dirty.discard(pane)
        pane.detach_library()
        group.removeTab(group.indexOf(pane))
        self.sessions.release(pane.session)
        pane.deleteLater()
        self._tidy_groups()
        self._update_title()

    def _on_crashed(self, pane: ReaderPane, message: str) -> None:
        if self._group_of(pane) is None:
            return
        QMessageBox.warning(self, "Bookwyrm", f"This tome could not be read.\n\n{message}")
        self.close_pane(pane)

    def _ask_password(self, pane: ReaderPane) -> None:
        text, ok = QInputDialog.getText(self, "Sealed tome", "This tome is sealed. Password:",
                                        QLineEdit.EchoMode.Password)
        if ok:
            pane.session.send_password(text)
        else:
            self.close_pane(pane)

    # -- remembering where you were ------------------------------------------------------

    def _mark_dirty(self, pane: ReaderPane) -> None:
        if self._released or self._group_of(pane) is None:
            return  # closed, or closing: there is nothing left to remember
        self._dirty.add(pane)
        self._save_timer.start()

    def _save_pane(self, pane: ReaderPane) -> None:
        if pane.book_id is None or not pane.view.page_count():
            return
        state = pane.state()
        self.library.save_reading_state(pane.book_id, state["page"], state["offset"],
                                        state["zoom"], state["mode"])

    def _save_reading_states(self) -> None:
        open_panes = set(self._panes())
        for pane in list(self._dirty):
            if pane in open_panes:
                self._save_pane(pane)
        self._dirty.clear()

    def release_library(self) -> bool:
        """Let go of the library: settle unsaved tomes, remember the session, close every
        tome and stop the helpers. Returns False if the user cancelled. After this the
        window is only good for closing (the library may be moved or swapped)."""
        if self._released:
            return True
        for pane in self._panes():  # settle every unsaved tome before anything closes
            if not self._may_close(pane):
                return False
        self._save_session()
        self.settings.setValue("window/geometry", self.saveGeometry())
        for pane in self._panes():
            self.close_pane(pane, ask=False)
        self.importer.close()
        self._save_timer.stop()
        self.library_page.detach()  # the catalogue may be closed under us from here on
        self._released = True
        return True

    def closeEvent(self, event) -> None:
        if not self.release_library():
            event.ignore()
            return
        super().closeEvent(event)
