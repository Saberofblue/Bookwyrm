"""Application bootstrap."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from bookwyrm import log, paths
from bookwyrm.db.repo import Library
from bookwyrm.ui.main_window import MainWindow
from bookwyrm.ui.theme import theme


def _parse(argv):
    parser = argparse.ArgumentParser(prog="bookwyrm", description="Bookwyrm PDF library")
    parser.add_argument("pdf", nargs="?", help="open this PDF without adding it to the library")
    # Developer aids: render the window to an image once it has settled, then quit.
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    parser.add_argument("--page", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--zoom", help=argparse.SUPPRESS)
    parser.add_argument("--size", default="1280x860", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _apply_debug_view(pane, args) -> None:
    view = pane.view
    if args.zoom == "fit-page":
        view.fit_page()
    elif args.zoom == "fit-width":
        view.fit_width()
    elif args.zoom:
        view.set_zoom(float(args.zoom))
    if args.page:
        view.go_to_page(args.page - 1)


def _screenshot_when_settled(window, is_settled, path: str, app) -> None:
    def poll():
        if is_settled():
            window.grab().save(path)
            window.close()
            app.quit()
        else:
            QTimer.singleShot(50, poll)
    QTimer.singleShot(250, poll)


def create_app(argv=None) -> QApplication:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("Bookwyrm")
    app.setOrganizationName("Bookwyrm")
    theme.apply(app)
    app.setWindowIcon(QIcon(str(theme.ASSETS / "icons" / "app-icon.png")))
    return app


class Shell:
    """Owns the open library and its window, and can swap the library for another."""

    def __init__(self, app: QApplication, size: tuple[int, int]):
        self.app = app
        self.size = size
        self.library: Library | None = None
        self.window: MainWindow | None = None

    def open(self, root: Path) -> MainWindow:
        root.mkdir(parents=True, exist_ok=True)
        log.install(str(root / "bookwyrm.log"))  # helpers inherit the location
        self.library = Library(root)
        self.window = MainWindow(self.library)
        self.window.libraryChangeRequested.connect(self.change_library)
        self.window.resize(*self.size)
        self.window.show()
        return self.window

    def change_library(self, folder: str, mode: str) -> None:
        """Move the library to `folder` ('move') or start using the one there ('switch')."""
        from bookwyrm.ui.settings import move_with_progress

        target, old_window, old_root = Path(folder), self.window, self.library.root
        if not old_window.release_library():
            return  # an unsaved tome was not settled: stay as we are
        self.library.close()
        problem = ""
        if mode == "move":
            problem = move_with_progress(old_root, target, old_window)
        if problem:  # nothing moved: carry on with the library where it was
            window = self.open(old_root)
            if problem != "cancelled":
                QMessageBox.warning(window, "Bookwyrm",
                                    f"Your library was not moved.\n\n{problem}")
        else:
            if not paths.env_override():
                QSettings().setValue("library_root", str(target))
            if mode == "move":  # the same library in a new place: its open tomes come along
                QSettings().setValue("session/library", str(target))
            window = self.open(target)
            window.notice(f"Your library now lives in {target}." if mode == "move"
                          else f"Now using the library in {target}.")
        old_window.close()
        old_window.deleteLater()

    def close(self) -> None:
        if self.library is not None:
            self.library.close()


def run(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    app = create_app()
    width, height = (int(n) for n in args.size.lower().split("x"))
    shell = Shell(app, (width, height))
    window = shell.open(paths.library_root(QSettings().value("library_root")))
    if args.pdf:
        pane = window.open_path(args.pdf)

        def ready():
            _apply_debug_view(pane, args)
            if args.screenshot:
                _screenshot_when_settled(window, pane.view.is_idle, args.screenshot, app)
        pane.view.loaded.connect(lambda: QTimer.singleShot(0, ready))
    elif args.screenshot:
        _screenshot_when_settled(window, lambda: not window.importer.busy(),
                                 args.screenshot, app)
    try:
        return app.exec()
    finally:
        shell.close()
