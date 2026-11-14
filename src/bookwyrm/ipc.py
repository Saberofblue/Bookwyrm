"""Talking to Bookwyrm's helper processes from the Qt side.

Helpers are plain subprocesses of the same executable (``--host`` / ``--worker``)
connected by a two-way pipe (see channel.py). They never re-import the app's main
module, so they behave the same from the command line, under tests and in a
packaged app.
"""

from __future__ import annotations

import subprocess

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage

from bookwyrm import channel


class _Reader(QThread):
    """Blocks on the socket so the GUI thread never does."""

    message = Signal(object)
    pixels = Signal(int, QImage)
    lost = Signal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self._conn = conn

    def run(self) -> None:
        conn = self._conn
        try:
            while True:
                msg = conn.recv()
                if msg[0] == "pixels":
                    _, req_id, width, height, stride = msg
                    data = conn.recv_bytes()
                    image = QImage(data, width, height, stride, QImage.Format.Format_RGB888)
                    # convertToFormat copies, so the image no longer borrows `data`.
                    self.pixels.emit(req_id, image.convertToFormat(QImage.Format.Format_RGB32))
                else:
                    self.message.emit(msg)
        except (EOFError, OSError):
            self.lost.emit()


class ProcessLink(QObject):
    """A helper process plus the socket to it."""

    message = Signal(object)  # every reply except pixel frames
    pixels = Signal(int, QImage)
    lost = Signal()  # the helper went away without being asked to

    def __init__(self, role: str, *args: str, parent: QObject | None = None):
        super().__init__(parent)
        self._closing = False
        self._process, self._conn = channel.start_helper(role, *args)
        self._reader = _Reader(self._conn, self)
        self._reader.message.connect(self.message)
        self._reader.pixels.connect(self.pixels)
        self._reader.lost.connect(self._on_lost)
        self._reader.start()

    def send(self, msg) -> None:
        try:
            self._conn.send(msg)
        except (OSError, ValueError):
            pass  # the helper is gone; `lost` reports it

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.send(("close",))
        try:
            self._process.wait(1.5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(1.0)
        try:
            self._conn.close()
        except OSError:
            pass
        self._reader.wait(1500)

    def _on_lost(self) -> None:
        if not self._closing:
            self.lost.emit()
