"""Printing a tome: pages are rendered by the document host and painted onto paper."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import QProgressDialog

MAX_DPI = 300  # sharper than this only makes the job bigger


class PrintJob(QObject):
    """Sends pages to a printer one at a time, so a long tome never fills memory."""

    progressed = Signal(int, int)  # pages done, pages in the job
    finished = Signal(bool, str)  # completed?, message when not

    def __init__(self, session, printer: QPrinter, pages: list[int], parent=None):
        super().__init__(parent)
        self._session = session
        self._printer = printer
        self._pages = list(pages)
        self._sizes = (session.info or {}).get("sizes", [])
        self._painter: QPainter | None = None
        self._request: int | None = None
        self._done = 0
        self._over = False
        session.pixels.connect(self._on_pixels)
        session.failed.connect(self._on_failed)

    def start(self) -> None:
        if not self._pages:
            self._finish(False, "There are no pages to print.")
            return
        self._painter = QPainter()
        if not self._painter.begin(self._printer):
            self._painter = None
            self._finish(False, "The printer could not be started.")
            return
        self._painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._ask()

    def cancel(self) -> None:
        if not self._over:
            self._printer.abort()
            self._finish(False, "Printing was cancelled.")

    def _ask(self) -> None:
        page = self._pages[self._done]
        width, height = self._sizes[page]
        scale = min(self._printer.resolution(), MAX_DPI) / 72.0
        self._request = self._session.tile(page, scale, (0, 0, width, height), 0)

    def _on_pixels(self, request: int, image) -> None:
        if self._over or request != self._request:
            return
        paper = QRectF(self._painter.viewport())
        fit = min(paper.width() / image.width(), paper.height() / image.height())
        width, height = image.width() * fit, image.height() * fit
        target = QRectF(paper.left() + (paper.width() - width) / 2,
                        paper.top() + (paper.height() - height) / 2, width, height)
        self._painter.drawImage(target, image)
        self._done += 1
        self.progressed.emit(self._done, len(self._pages))
        if self._done >= len(self._pages):
            self._finish(True, "")
        else:
            self._printer.newPage()
            self._ask()

    def _on_failed(self, request: int, message: str) -> None:
        if not self._over and request == self._request:
            self._printer.abort()
            self._finish(False, f"A page could not be prepared for printing: {message}")

    def _finish(self, ok: bool, message: str) -> None:
        if self._over:
            return
        self._over = True
        if self._painter is not None:
            self._painter.end()
        self.finished.emit(ok, message)


def pages_to_print(printer: QPrinter, count: int, current: int) -> list[int]:
    """The 0-based pages a print dialog asked for."""
    mode = printer.printRange()
    if mode == QPrinter.PrintRange.CurrentPage:
        return [current]
    if mode == QPrinter.PrintRange.PageRange:
        first = max(1, printer.fromPage())
        last = min(count, printer.toPage() or count)
        return list(range(first - 1, last))
    return list(range(count))


def print_tome(pane, title: str) -> None:
    """Ask how to print the tome in `pane`, then print it with a progress window."""
    count = pane.view.page_count()
    if not count:
        return
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setDocName(title)
    dialog = QPrintDialog(printer, pane)
    dialog.setWindowTitle(f"Print {title}")
    dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintPageRange, True)
    dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage, True)
    dialog.setMinMax(1, count)
    dialog.setFromTo(1, count)
    if dialog.exec() != QPrintDialog.DialogCode.Accepted:
        return
    pages = pages_to_print(printer, count, pane.view.current_page())
    progress = QProgressDialog(f"Printing {title}…", "Cancel", 0, len(pages), pane)
    progress.setWindowTitle("Printing")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(600)
    job = PrintJob(pane.session, printer, pages, pane)
    job.progressed.connect(lambda done, _total: progress.setValue(done))
    progress.canceled.connect(job.cancel)

    def finished(ok: bool, message: str) -> None:
        progress.close()
        pane.notice.emit(f"Sent {len(pages)} page{'s' if len(pages) != 1 else ''} to the "
                         "printer." if ok else message)
        job.deleteLater()
    job.finished.connect(finished)
    pane._print_job = job  # keep it alive while it runs
    job.start()
