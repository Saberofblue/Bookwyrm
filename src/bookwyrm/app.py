"""Application bootstrap."""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

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


def run(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    app = create_app()
    root = paths.library_root(QSettings().value("library_root"))
    root.mkdir(parents=True, exist_ok=True)
    log.install(str(root / "bookwyrm.log"))  # helpers inherit the location
    library = Library(root)
    window = MainWindow(library)
    width, height = (int(n) for n in args.size.lower().split("x"))
    window.resize(width, height)
    window.show()
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
        library.close()
