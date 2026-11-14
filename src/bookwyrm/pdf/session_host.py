"""Document host: the one place PyMuPDF touches an open tome.

Runs in its own process because PyMuPDF is single-threaded and holds the GIL
while rendering. Speaks plain tuples over a multiprocessing Connection and never
imports Qt.

Requests (client -> host)
    ("tile", req_id, page, scale, (x0, y0, x1, y1), priority)
    ("preview", req_id, page, width_px, priority)
    ("cancel", [req_id, ...])
    ("words", req_id, page)                      text selection data for one page
    ("search", req_id, needle, start_page)       find in document; replaces any earlier search
    ("stop_search",)
    ("annots", req_id, page)                     annotations on a page (for selecting them)
    ("widgets", req_id, page)                    form fields on a page
    ("text_lines", req_id, page)                 lines of text with their style (for editing)
    ("images", req_id, page)                     pictures placed on a page
    ("image_uses", req_id, xref)                 how many pages show a stored picture
    ("edit", req_id, name, args)                 one undoable change (see pdf/editing.py)
    ("undo", req_id) / ("redo", req_id)
    ("save", req_id, archive_path | None)        write to disk; history starts afresh
    ("restore", req_id, archive_path)            put the archive copy back
    ("extract", req_id, pages, destination)      write some pages to a new PDF
    ("password", text)
    ("close",)

Replies (host -> client)
    ("opened", info)                      info: see Host.info()
    ("needs_password",)
    ("pixels", req_id, width, height, stride)   followed by one raw RGB bytes frame
    ("reply", req_id, payload)            words: [(x0, y0, x1, y1, text, block, line, n), ...]
    ("found", req_id, page, [(x0, y0, x1, y1), ...])   one per page that has matches
    ("search_done", req_id, total_matches)
    ("edited", req_id, state, result)     after edit / undo / redo; state: see Host.edit_state()
    ("saved", req_id, report)             report: file_size, sha256, page_count, mapping, state
    ("failed", req_id, message)
    ("error", message)                    fatal; the host exits
"""

from __future__ import annotations

from collections import OrderedDict

DISPLAY_LIST_CACHE = 8
SEARCH_CHUNK = 6  # pages searched between checks for more urgent work
SEARCH_LIMIT = 5000  # matches; beyond this the needle is too common to be useful


class Host:
    def __init__(self, conn, path: str):
        import pymupdf

        from bookwyrm.pdf import compat, editing

        compat.apply()
        self.pymupdf = pymupdf
        self.editing = editing
        self.conn = conn
        self.path = path
        self.editor = editing.Editor(pymupdf.open(path), path)
        self.revision = 0
        self._lists: OrderedDict[int, object] = OrderedDict()

    @property
    def doc(self):
        """The open document. Saving reopens it, so always go through the editor."""
        return self.editor.doc

    # -- document facts -------------------------------------------------

    def info(self) -> dict:
        doc = self.doc
        sizes = []
        for i in range(doc.page_count):
            rect = doc[i].rect
            sizes.append((rect.width, rect.height))
        meta = doc.metadata or {}
        return {
            "path": self.path,
            "page_count": doc.page_count,
            "sizes": sizes,
            "toc": doc.get_toc(simple=True),
            "title": meta.get("title") or "",
            "author": meta.get("author") or "",
            "revision": self.revision,
            "is_pdf": doc.is_pdf,
            "is_form": bool(doc.is_form_pdf),
            "metadata": self.metadata(),
            "edit": self.editor.summary(),
        }

    def metadata(self) -> dict:
        meta = self.doc.metadata or {}
        return {key: meta.get(key) or "" for key in ("title", "author", "subject", "keywords")}

    # -- editing --------------------------------------------------------

    def edit_state(self, entry: dict | None) -> dict:
        """What views need to know after a change. `entry` is the editor's log entry."""
        doc = self.doc
        structural = True if entry is None else entry["structural"]
        state = dict(self.editor.summary())
        state.update(
            revision=self.revision,
            changed=None if entry is None else entry["pages"],
            structural=structural,
            toc=doc.get_toc(simple=True),
            metadata=self.metadata(),
            page_count=doc.page_count,
            sizes=None,
            origins=list(self.editor.origins),
        )
        if structural:
            state["sizes"] = [(doc[i].rect.width, doc[i].rect.height)
                              for i in range(doc.page_count)]
        return state

    def after_change(self, entry: dict | None) -> dict:
        self.revision += 1
        pages = None if entry is None else entry["pages"]
        if entry is None or entry["structural"] or pages is None:
            self.invalidate()
        else:
            for page in pages:
                self.invalidate(page)
        return self.edit_state(entry)

    def release_file(self) -> None:
        """Let go of everything that could keep the tome's file open.

        Saving swaps a new file in for the old one, which Windows only allows once
        nothing holds the old one open. Cached display lists can do that through
        the images they refer to, so they go before the document is closed.
        """
        import gc

        self.invalidate()
        gc.collect()

    def handle_edit(self, msg) -> None:
        """Run an edit, undo, redo or save and report the outcome."""
        op, req_id = msg[0], msg[1]
        try:
            if op == "edit":
                entry = self.editor.apply(msg[2], msg[3])
                self.conn.send(("edited", req_id, self.after_change(entry), entry["result"]))
            elif op in ("undo", "redo"):
                entry = self.editor.undo() if op == "undo" else self.editor.redo()
                if entry is None:
                    raise self.editing.EditError(f"There is nothing to {op}.")
                self.conn.send(("edited", req_id, self.after_change(entry), None))
            elif op == "save":
                self.release_file()
                report = self.editor.save(msg[2])
                self.invalidate()  # the document object was replaced
                report["state"] = self.editor.summary()
                self.conn.send(("saved", req_id, report))
            elif op == "restore":
                self.release_file()
                report = self.editor.restore(msg[2])
                state = self.after_change(None)  # every page may differ: lay out afresh
                report["state"] = self.editor.summary()
                self.conn.send(("edited", req_id, state, None))
                self.conn.send(("saved", req_id, report))
            elif op == "extract":
                count = self.editing.extract_pages(self.doc, msg[2], msg[3])
                self.conn.send(("reply", req_id, count))
        except self.editing.EditError as problem:
            if op in ("save", "restore"):
                # A failed save reopens the tome and clears its history; tell the views.
                self.invalidate()
                self.conn.send(("edited", req_id,
                                self.after_change({"pages": [], "structural": False}), None))
            self.conn.send(("failed", req_id, str(problem)))
        except Exception as exc:
            self.conn.send(("failed", req_id, f"{type(exc).__name__}: {exc}"))

    # -- rendering ------------------------------------------------------

    def display_list(self, page: int):
        cached = self._lists.get(page)
        if cached is not None:
            self._lists.move_to_end(page)
            return cached
        dl = self.doc[page].get_displaylist()
        self._lists[page] = dl
        while len(self._lists) > DISPLAY_LIST_CACHE:
            self._lists.popitem(last=False)
        return dl

    def invalidate(self, page: int | None = None) -> None:
        if page is None:
            self._lists.clear()
        else:
            self._lists.pop(page, None)

    def render_tile(self, page: int, scale: float, clip):
        pm = self.pymupdf
        return self.display_list(page).get_pixmap(
            matrix=pm.Matrix(scale, scale), clip=pm.Rect(*clip), alpha=False
        )

    def render_preview(self, page: int, width_px: int):
        pm = self.pymupdf
        dl = self.display_list(page)
        scale = width_px / max(1.0, dl.rect.width)
        return dl.get_pixmap(matrix=pm.Matrix(scale, scale), alpha=False)

    def send_pixmap(self, req_id: int, pix) -> None:
        self.conn.send(("pixels", req_id, pix.width, pix.height, pix.stride))
        self.conn.send_bytes(pix.samples_mv)

    # -- text -----------------------------------------------------------

    def words(self, page: int) -> list:
        return [tuple(word) for word in self.doc[page].get_text("words")]

    def search(self, req_id: int, needle: str, start_page: int):
        """Search outward from `start_page`, wrapping round. Yields between chunks."""
        count = self.doc.page_count
        total = 0
        for step in range(count):
            page = (start_page + step) % count
            try:
                rects = self.doc[page].search_for(needle)
            except Exception:
                rects = []
            if rects:
                total += len(rects)
                self.conn.send(("found", req_id, page, [(r.x0, r.y0, r.x1, r.y1) for r in rects]))
                if total >= SEARCH_LIMIT:
                    break
            if step % SEARCH_CHUNK == SEARCH_CHUNK - 1:
                yield
        self.conn.send(("search_done", req_id, total))

    # -- main loop ------------------------------------------------------

    def serve(self) -> None:
        conn = self.conn
        jobs: dict[int, tuple] = {}  # req_id -> (priority, -arrival, message)
        arrival = 0
        searching = None  # generator of the find-in-document under way
        while True:
            idle = not jobs and searching is None
            messages = [conn.recv()] if idle else []
            while conn.poll():
                messages.append(conn.recv())
            for msg in messages:
                op = msg[0]
                if op in ("tile", "preview"):
                    arrival += 1
                    jobs[msg[1]] = (msg[-1], -arrival, msg)
                elif op == "cancel":
                    for req_id in msg[1]:
                        jobs.pop(req_id, None)
                elif op == "words":
                    try:
                        conn.send(("reply", msg[1], self.words(msg[2])))
                    except Exception as exc:
                        conn.send(("failed", msg[1], f"{type(exc).__name__}: {exc}"))
                elif op in ("annots", "widgets", "text_lines", "images", "image_uses"):
                    try:
                        lister = {
                            "annots": self.editing.list_annots,
                            "widgets": self.editing.list_widgets,
                            "text_lines": self.editing.list_text_lines,
                            "images": self.editing.list_images,
                            "image_uses": self.editing.image_uses,
                        }[op]
                        conn.send(("reply", msg[1], lister(self.doc, msg[2])))
                    except Exception as exc:
                        conn.send(("failed", msg[1], f"{type(exc).__name__}: {exc}"))
                elif op in ("edit", "undo", "redo", "save", "restore", "extract"):
                    if op != "extract":
                        searching = None  # match boxes would be stale after a change
                    self.handle_edit(msg)
                elif op == "search":
                    searching = self.search(msg[1], msg[2], msg[3])
                elif op == "stop_search":
                    searching = None
                elif op == "close":
                    return
            if jobs:
                # Most urgent first; among equals, the most recently requested.
                req_id = min(jobs, key=lambda k: jobs[k][:2])
                msg = jobs.pop(req_id)[2]
                try:
                    if msg[0] == "tile":
                        pix = self.render_tile(msg[2], msg[3], msg[4])
                    else:
                        pix = self.render_preview(msg[2], msg[3])
                    self.send_pixmap(req_id, pix)
                except Exception as exc:  # one bad page must not kill the tome
                    conn.send(("failed", req_id, f"{type(exc).__name__}: {exc}"))
            elif searching is not None:
                try:
                    next(searching)
                except StopIteration:
                    searching = None


def main(conn, path: str) -> None:
    """Process entry point."""
    try:
        host = Host(conn, path)
        while host.doc.needs_pass:
            conn.send(("needs_password",))
            msg = conn.recv()
            if msg[0] == "close":
                return
            if msg[0] == "password":
                if host.doc.authenticate(msg[1]):
                    host.editor.password = msg[1]
        conn.send(("opened", host.info()))
        host.serve()
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass  # the app went away; nothing to report to
    except Exception as exc:
        from bookwyrm import log

        log.record_exception(f"the reader helper could not continue with {path}")
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
    finally:
        conn.close()


def run(pipe: int, path: str) -> int:
    """Entry used by ``bookwyrm --host <pipe number> <path>``."""
    from bookwyrm import channel

    main(channel.connect(pipe), path)
    return 0
