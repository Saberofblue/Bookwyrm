"""Brings PDFs into the library and keeps the search index filled.

Orchestrates the worker process: inspect (hash + facts) -> copy into the
library and render a cover -> extract page text for the Card Catalogue, and
note the numbers printed on the pages. All catalogue writes happen here, on
the GUI side, so there is a single writer.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from bookwyrm.db.repo import Library
from bookwyrm.ipc import ProcessLink

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)),
             *(f"lpt{n}" for n in range(1, 10))}  # device names Windows will not accept


def safe_filename(path: str, limit: int = 120) -> str:
    """A file name every system accepts: no reserved characters or names, not too long."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(path).name)
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = stem.strip(" .")[:limit].rstrip(" .") or "tome"
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    return stem + ".pdf"


class Importer(QObject):
    status = Signal(str)  # what the Librarian is doing; '' when idle
    bookReady = Signal(int)  # a tome's file and cover are now in the library
    batchFinished = Signal(dict)  # {"added": [...], "duplicates": [...], "failed": [...]}

    def __init__(self, library: Library, parent: QObject | None = None):
        super().__init__(parent)
        self.library = library
        self._link: ProcessLink | None = None
        self._ids = itertools.count(1)
        self._jobs: dict[int, dict] = {}
        self._files_left = 0
        self._summary = self._new_summary()
        self._closed = False

    @staticmethod
    def _new_summary() -> dict:
        return {"added": [], "duplicates": [], "failed": []}

    # -- public -------------------------------------------------------------

    def import_files(self, paths, place_id: int | None = None) -> None:
        """Add PDFs to the library, shelving them in `place_id` (None = Returns Cart)."""
        for path in paths:
            self._files_left += 1
            self._start({"kind": "inspect", "path": str(path), "place_id": place_id},
                        ("inspect", str(path)))
        self._announce()

    def reindex(self, book_id: int) -> None:
        """Re-read a tome's text, e.g. after it was edited."""
        book = self.library.book(book_id)
        if book is None or not book.ready:
            return
        self.forget(book_id)
        self.library.clear_text(book_id)
        self._start({"kind": "index", "book_id": book_id, "title": book.title, "done": 0,
                     "pages": book.page_count},
                    ("index", str(self.library.book_file(book))))
        self._announce()

    def resume(self) -> None:
        """Finish anything a previous session left half-done."""
        self.library.discard_unfinished()
        for book in self.library.unindexed_books():
            self.reindex(book.id)
        # Tomes added before Bookwyrm kept printed page numbers: look for theirs now. (A tome
        # being read again above reports its numbers when the reading is done.)
        reading = {state.get("book_id") for state in self._jobs.values()}
        for book in self.library.books_without_page_labels():
            if book.id not in reading:
                self._start({"kind": "labels", "book_id": book.id},
                            ("labels", str(self.library.book_file(book))))

    def forget(self, book_id: int) -> None:
        """Stop working on a tome (it is being removed or re-read)."""
        for job, state in list(self._jobs.items()):
            if state.get("book_id") == book_id:
                self._send(("cancel", job))
                del self._jobs[job]
        self._announce()

    def busy(self) -> bool:
        return bool(self._jobs)

    def close(self) -> None:
        """Stop the worker. Anything it had already sent is ignored from here on, because
        the catalogue may be closed straight after."""
        self._closed = True
        self._jobs.clear()
        if self._link is not None:
            self._link.close()
            self._link = None

    # -- worker plumbing ------------------------------------------------------

    def _send(self, msg) -> None:
        if self._link is None:
            self._link = ProcessLink("--worker", parent=self)
            self._link.message.connect(self._on_message)
            self._link.lost.connect(self._on_lost)
        self._link.send(msg)

    def _start(self, state: dict, request: tuple) -> int:
        job = next(self._ids)
        self._jobs[job] = state
        self._send((request[0], job, *request[1:]))
        return job

    def _on_lost(self) -> None:
        """The worker died: fail what it was doing and let the next request restart it."""
        if self._closed:
            return
        self._link = None
        for state in self._jobs.values():
            if state["kind"] in ("inspect", "ingest"):
                self._file_done("failed", (state["path"], "the cataloguing process stopped"))
                if state["kind"] == "ingest":
                    self.library.delete_book(state["book_id"])
        self._jobs.clear()
        self._announce()

    def _on_message(self, msg) -> None:
        if self._closed:
            return
        op, job = msg[0], msg[1]
        state = self._jobs.get(job)
        if state is None:
            return
        if op == "inspected":
            del self._jobs[job]
            self._inspected(state, msg[2])
        elif op == "ingested":
            del self._jobs[job]
            self._ingested(state)
        elif op == "text":
            if self.library.book(state["book_id"]) is not None:
                self.library.add_page_text(state["book_id"], msg[2])
            state["done"] += len(msg[2])
        elif op == "indexed":
            del self._jobs[job]
            if self.library.book(state["book_id"]) is not None:
                self.library.set_page_labels(state["book_id"], msg[3] if len(msg) > 3 else None)
                self.library.set_indexed(state["book_id"], True)
        elif op == "labels":
            del self._jobs[job]
            if self.library.book(state["book_id"]) is not None:
                self.library.set_page_labels(state["book_id"], msg[2])
        elif op == "job_failed":
            del self._jobs[job]
            self._failed(state, msg[2])
        self._announce()

    # -- steps ------------------------------------------------------------------

    def _inspected(self, state: dict, facts: dict) -> None:
        library, place_id = self.library, state["place_id"]
        existing = library.find_by_hash(facts["sha256"])
        if existing is not None:
            if place_id is not None:
                library.place_book(existing.id, place_id)
            self._file_done("duplicates", (state["path"], existing.id))
            return
        book = library.add_book(
            title=facts["title"], author=facts["author"], filename=safe_filename(state["path"]),
            sha256=facts["sha256"], page_count=facts["page_count"], file_size=facts["file_size"],
        )
        if place_id is not None and library.place(place_id) is not None:
            library.place_book(book.id, place_id)
        self._start(
            {"kind": "ingest", "path": state["path"], "book_id": book.id},
            ("ingest", state["path"], str(library.book_file(book)),
             str(library.cover_file(book.id))),
        )

    def _ingested(self, state: dict) -> None:
        book_id = state["book_id"]
        book = self.library.book(book_id)
        if book is None:  # removed while it was being copied
            self._file_done("failed", (state["path"], "removed during import"))
            return
        self.library.mark_ready(book_id)
        self.bookReady.emit(book_id)
        self._file_done("added", book_id)
        self._start({"kind": "index", "book_id": book_id, "title": book.title, "done": 0,
                     "pages": book.page_count},
                    ("index", str(self.library.book_file(book))))

    def _failed(self, state: dict, message: str) -> None:
        if state["kind"] == "ingest":
            self.library.delete_book(state["book_id"])
        if state["kind"] in ("inspect", "ingest"):
            self._file_done("failed", (state["path"], message))
        # a failed index simply leaves the tome unindexed; resume() retries next launch

    def _file_done(self, outcome: str, detail) -> None:
        self._summary[outcome].append(detail)
        self._files_left -= 1
        if self._files_left <= 0:
            self._files_left = 0
            summary, self._summary = self._summary, self._new_summary()
            self.batchFinished.emit(summary)

    # -- status line ----------------------------------------------------------------

    def _announce(self) -> None:
        if self._files_left:
            noun = "tome" if self._files_left == 1 else "tomes"
            self.status.emit(f"The Librarian is cataloguing {self._files_left} {noun}…")
            return
        reading = [s for s in self._jobs.values() if s["kind"] == "index"]
        if not reading:
            self.status.emit("")
            return
        current = max(reading, key=lambda s: s["done"])
        text = f"The Librarian is reading “{current['title']}”"
        if current["pages"]:
            text += f" — page {min(current['done'], current['pages'])} of {current['pages']}"
        if len(reading) > 1:
            text += f" ({len(reading) - 1} more waiting)"
        self.status.emit(text)
