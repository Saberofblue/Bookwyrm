"""Qt-side handle on a document host process."""

from __future__ import annotations

import itertools

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from bookwyrm.ipc import ProcessLink


class DocumentSession(QObject):
    """One open tome. All views of the same tome share one session."""

    opened = Signal(dict)
    pixels = Signal(int, QImage)
    failed = Signal(int, str)
    reply = Signal(int, object)  # answer to words()
    found = Signal(int, int, list)  # search id, page, [(x0, y0, x1, y1), ...]
    searchDone = Signal(int, int)  # search id, total matches
    edited = Signal(int, dict, object)  # request id, state after the change, result
    saved = Signal(int, dict)  # request id, report
    password_required = Signal()
    crashed = Signal(str)

    def __init__(self, path: str, parent: QObject | None = None):
        super().__init__(parent)
        self.path = str(path)
        self.info: dict | None = None
        self.edit_state: dict = {"dirty": False, "can_undo": False, "can_redo": False,
                                 "undo_name": "", "redo_name": ""}
        self._ids = itertools.count(1)
        self._link = ProcessLink("--host", self.path, parent=self)
        self._link.message.connect(self._on_message)
        self._link.pixels.connect(self.pixels)
        self._link.lost.connect(
            lambda: self.crashed.emit("The tome's reader process stopped unexpectedly."))

    # -- requests ---------------------------------------------------------

    def tile(self, page: int, scale: float, clip: tuple, priority: int = 0) -> int:
        req_id = next(self._ids)
        self._link.send(("tile", req_id, page, scale, clip, priority))
        return req_id

    def preview(self, page: int, width_px: int, priority: int = 0) -> int:
        req_id = next(self._ids)
        self._link.send(("preview", req_id, page, width_px, priority))
        return req_id

    def cancel(self, req_ids) -> None:
        req_ids = list(req_ids)
        if req_ids:
            self._link.send(("cancel", req_ids))

    def words(self, page: int) -> int:
        req_id = next(self._ids)
        self._link.send(("words", req_id, page))
        return req_id

    def search(self, needle: str, start_page: int = 0) -> int:
        """Start a find-in-document; any earlier search is abandoned."""
        req_id = next(self._ids)
        self._link.send(("search", req_id, needle, start_page))
        return req_id

    def stop_search(self) -> None:
        self._link.send(("stop_search",))

    def annots(self, page: int) -> int:
        req_id = next(self._ids)
        self._link.send(("annots", req_id, page))
        return req_id

    def widgets(self, page: int) -> int:
        req_id = next(self._ids)
        self._link.send(("widgets", req_id, page))
        return req_id

    def query(self, what: str, subject: int) -> int:
        """Ask about a page ('text_lines', 'images', 'links') or a picture ('image_uses').

        The answer arrives through `reply`.
        """
        req_id = next(self._ids)
        self._link.send((what, req_id, subject))
        return req_id

    # -- editing: every change is one undoable step in the host ---------------

    def edit(self, name: str, **args) -> int:
        req_id = next(self._ids)
        self._link.send(("edit", req_id, name, args))
        return req_id

    def undo(self) -> int:
        req_id = next(self._ids)
        self._link.send(("undo", req_id))
        return req_id

    def redo(self) -> int:
        req_id = next(self._ids)
        self._link.send(("redo", req_id))
        return req_id

    def save(self, archive_path: str | None = None) -> int:
        req_id = next(self._ids)
        self._link.send(("save", req_id, archive_path))
        return req_id

    def restore(self, archive_path: str) -> int:
        """Replace the tome with its archive copy (answers with `edited` then `saved`)."""
        req_id = next(self._ids)
        self._link.send(("restore", req_id, archive_path))
        return req_id

    def extract(self, pages, destination: str) -> int:
        """Write some pages to a new PDF; answers with `reply(req_id, count)`."""
        req_id = next(self._ids)
        self._link.send(("extract", req_id, list(pages), destination))
        return req_id

    @property
    def dirty(self) -> bool:
        return bool(self.edit_state.get("dirty"))

    def send_password(self, password: str) -> None:
        self._link.send(("password", password))

    def close(self) -> None:
        self._link.close()

    # -- internals --------------------------------------------------------

    def _absorb(self, state: dict) -> None:
        """Keep `info` current so views opened later see the edited tome."""
        self.edit_state = {key: state.get(key) for key in
                           ("dirty", "can_undo", "can_redo", "undo_name", "redo_name")}
        if self.info is None:
            return
        for key in ("toc", "page_count", "revision", "metadata"):
            if state.get(key) is not None:
                self.info[key] = state[key]
        if state.get("metadata"):
            self.info["title"] = state["metadata"].get("title", "")
            self.info["author"] = state["metadata"].get("author", "")
        if state.get("sizes") is not None:
            self.info["sizes"] = state["sizes"]
            self.info["labels"] = state.get("labels")

    def _on_message(self, msg) -> None:
        op = msg[0]
        if op == "opened":
            self.info = msg[1]
            self.edit_state = dict(self.info.get("edit") or self.edit_state)
            self.opened.emit(self.info)
        elif op == "edited":
            state = msg[2]
            self._absorb(state)
            self.edited.emit(msg[1], state, msg[3])
        elif op == "saved":
            report = msg[2]
            self._absorb(report["state"])
            if self.info is not None:
                self.info["page_count"] = report["page_count"]
            self.saved.emit(msg[1], report)
        elif op == "failed":
            self.failed.emit(msg[1], msg[2])
        elif op == "reply":
            self.reply.emit(msg[1], msg[2])
        elif op == "found":
            self.found.emit(msg[1], msg[2], msg[3])
        elif op == "search_done":
            self.searchDone.emit(msg[1], msg[2])
        elif op == "needs_password":
            self.password_required.emit()
        elif op == "error":
            self.crashed.emit(msg[1])
