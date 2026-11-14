"""Bookwyrm checks itself: ``bookwyrm --self-test [report.txt]``.

Exercises the parts most likely to differ between computers: the helper
processes, rendering, editing and saving a tome, importing and searching. It
uses a throwaway library in a temporary folder, shows no window, and never
touches the real library. Each check prints ``[ OK ]`` or ``[FAIL]`` with the
error, so a report from a machine nobody can reach says exactly what broke.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

PAGES = 5
WORD = "wyvernword"  # appears once per page of the sample tome; searched for later


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.checks = 0
        self.failed = 0

    def say(self, text: str = "") -> None:
        self.lines.append(text)
        print(text, flush=True)

    def check(self, name: str, fn) -> bool:
        self.checks += 1
        start = time.perf_counter()
        try:
            detail = fn()
        except Exception:
            self.failed += 1
            self.say(f"[FAIL] {name}")
            for line in traceback.format_exc().rstrip().splitlines():
                self.say("       " + line)
            return False
        took = (time.perf_counter() - start) * 1000
        self.say(f"[ OK ] {name} ({took:.0f} ms)" + (f": {detail}" if detail else ""))
        return True


def run(report_path: str | None = None) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no window appears
    report = Report()
    work = Path(tempfile.mkdtemp(prefix="bookwyrm-selftest-"))
    os.environ["BOOKWYRM_LOG"] = str(work / "bookwyrm.log")
    report.say("Bookwyrm self-test")
    report.say(f"  system:  {platform.platform()} ({platform.machine()})")
    report.say(f"  python:  {sys.version.split()[0]} at {sys.executable}")
    report.say(f"  packaged app: {'yes' if getattr(sys, 'frozen', False) else 'no'}")
    report.say("")
    try:
        _run_checks(report, work)
    except Exception:  # a bug in the self-test itself must still produce a report
        report.failed += 1
        report.say("[FAIL] the self-test stopped unexpectedly")
        for line in traceback.format_exc().rstrip().splitlines():
            report.say("       " + line)
    log = work / "bookwyrm.log"
    if log.exists() and log.stat().st_size:
        report.say("")
        report.say("Errors logged by helper processes:")
        for line in log.read_text("utf-8", errors="replace").rstrip().splitlines()[-60:]:
            report.say("  " + line)
    shutil.rmtree(work, ignore_errors=True)
    report.say("")
    if report.failed:
        report.say(f"RESULT: {report.failed} of {report.checks} checks FAILED")
    else:
        report.say(f"RESULT: all {report.checks} checks passed")
    if report_path:
        try:
            Path(report_path).write_text("\n".join(report.lines) + "\n", "utf-8")
        except OSError:
            pass
    return 1 if report.failed else 0


def _run_checks(report: Report, work: Path) -> None:
    state: dict = {"problems": []}

    def wait(done, seconds: float, what: str) -> None:
        """Let the app run until `done()` is true; fail loudly on a timeout or a reported error."""
        deadline = time.monotonic() + seconds
        while not done():
            if state["problems"]:
                raise RuntimeError(f"while waiting for {what}: {state['problems'].pop(0)}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"gave up after {seconds:.0f} s waiting for {what}")
            state["app"].processEvents()
            time.sleep(0.01)

    def versions():
        import pymupdf
        from PySide6.QtCore import qVersion
        return f"Qt {qVersion()}, PyMuPDF {pymupdf.__version__}"

    def sample_tome():
        import pymupdf
        path = work / "Self-Test Tome.pdf"
        doc = pymupdf.open()
        for number in range(PAGES):
            page = doc.new_page(width=420, height=595)
            page.insert_text((50, 80), f"Bookwyrm self-test, page {number + 1}", fontsize=16)
            page.insert_text((50, 110), f"{WORD} silver dragon hoard", fontsize=11)
            page.draw_rect(pymupdf.Rect(50, 140, 370, 300), color=(0.2, 0.3, 0.6),
                           fill=(0.75, 0.85, 0.95))
        doc.set_toc([[1, "Opening", 1], [1, "Closing", PAGES]])
        doc.save(path)
        doc.close()
        state["pdf"] = path
        return f"{path.stat().st_size} bytes"

    def window_toolkit():
        from bookwyrm.app import create_app
        from bookwyrm.ui.theme import icons, theme
        state["app"] = create_app()
        if icons.pixmap("wyvern", 32).isNull():
            raise RuntimeError("the bundled icons could not be drawn")
        fonts = f"{theme.DISPLAY_FONT} and {theme.BODY_FONT}"
        if theme.DISPLAY_FONT != "Cinzel" or theme.BODY_FONT != "EB Garamond":
            raise RuntimeError(f"the bundled fonts did not load (using {fonts})")
        return f"fonts {fonts}, platform plugin {state['app'].platformName()}"

    def catalogue():
        from bookwyrm.db.repo import Library
        library = Library(work / "library")
        case = library.add_place("bookcase", "Self-Test")
        state["library"] = library
        state["shelf"] = library.add_place("shelf", "Checks", case.id)
        return "bookcase and shelf created"

    def import_and_search():
        from bookwyrm.library.importer import Importer
        library = state["library"]
        importer = state["importer"] = Importer(library)
        summary = {}
        importer.batchFinished.connect(summary.update)
        importer.import_files([state["pdf"]], state["shelf"].id)
        wait(lambda: bool(summary), 60, "the import helper to copy the tome")
        if summary.get("failed"):
            raise RuntimeError(f"import failed: {summary['failed']}")
        wait(lambda: not importer.busy(), 60, "the import helper to read the tome's text")
        book = state["book"] = library.all_books()[0]
        if not (book.ready and book.indexed and book.page_count == PAGES):
            raise RuntimeError(f"unexpected catalogue entry: {book}")
        if not library.cover_file(book.id).exists():
            raise RuntimeError("no cover picture was made")
        pages = sorted(hit.page for hit in library.search(WORD))
        if pages != list(range(PAGES)):
            raise RuntimeError(f"search found pages {pages}")
        return f"copied, cover made, {PAGES} pages searchable"

    def open_tome():
        from bookwyrm.pdf.session_client import DocumentSession
        library, book = state["library"], state["book"]
        session = state["session"] = DocumentSession(str(library.book_file(book)))
        session.crashed.connect(lambda text: state["problems"].append(f"reader helper: {text}"))
        session.failed.connect(
            lambda _req, text: state["problems"].append(f"request failed: {text}"))
        wait(lambda: session.info is not None, 60, "the reader helper to open the tome")
        if session.info["page_count"] != PAGES:
            raise RuntimeError(f"opened with {session.info['page_count']} pages")
        return f"{PAGES} pages, {len(session.info['toc'])} contents entries"

    def render():
        session = state["session"]
        got = {}
        session.pixels.connect(lambda request, image: got.update({request: image}))
        request = session.tile(0, 2.0, (0, 0, 200, 200), 0)
        wait(lambda: request in got, 60, "a rendered page tile")
        image = got[request]
        if (image.width(), image.height()) != (400, 400):
            raise RuntimeError(f"tile was {image.width()}x{image.height()}")
        colours = {image.pixel(x, y) for x in range(0, 400, 20) for y in range(0, 400, 20)}
        if len(colours) < 2:
            raise RuntimeError("the rendered tile is blank")
        return "400x400 tile with content"

    def edit_and_save():
        import pymupdf
        session, library, book = state["session"], state["library"], state["book"]
        path = library.book_file(book)
        original = path.read_bytes()
        done: dict = {}
        session.edited.connect(lambda request, *_: done.update({request: True}))
        session.saved.connect(lambda request, report: done.update({request: report}))

        def finish(request, what):
            wait(lambda: request in done, 60, what)

        finish(session.edit("note", page=0, point=(60, 60), text="self-test"), "a note")
        finish(session.edit("add_text", page=1, point=(50, 400), text="Added by the self-test"),
               "added text")
        finish(session.edit("textbox", page=2, rect=(50, 330, 300, 370), text="A text box"),
               "a text box")
        finish(session.undo(), "undo")
        finish(session.redo(), "redo")
        if not session.dirty:
            raise RuntimeError("the tome does not report unsaved changes")
        archive = library.archive_file(book.id)
        finish(session.save(str(archive)), "the save")
        if session.dirty:
            raise RuntimeError("the tome still reports unsaved changes after saving")
        if archive.read_bytes() != original:
            raise RuntimeError("the archive copy is not the original file")
        saved = pymupdf.open(path)
        try:
            notes = [a.type[1] for a in saved[0].annots()]
            added = "Added by the self-test" in saved[1].get_text()
        finally:
            saved.close()
        if notes != ["Text"] or not added:
            raise RuntimeError(f"saved file has notes={notes}, added text={added}")
        finish(session.edit("note", page=3, point=(60, 60), text="second round"), "a later note")
        finish(session.save(str(archive)), "a second save")
        return "annotated, undone, redone, saved twice; original archived"

    def close_and_remove():
        library, book = state["library"], state["book"]
        state.pop("session").close()
        state["importer"].forget(book.id)
        folder = library.book_dir(book.id)
        library.delete_book(book.id)
        for _ in range(30):  # a helper may need a moment to let go of the file
            if not folder.exists():
                break
            time.sleep(0.1)
            shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise RuntimeError(f"the tome's folder could not be deleted: {folder}")
        return "helpers closed, files deleted"

    try:
        if not report.check("software versions", versions):
            return
        if not report.check("make a sample tome", sample_tome):
            return
        if not report.check("window toolkit, fonts and icons", window_toolkit):
            return
        if not report.check("catalogue database", catalogue):
            return
        if report.check("import helper: copy, cover, text search", import_and_search):
            if report.check("reader helper: open a tome", open_tome):
                report.check("render a page", render)
                report.check("edit, undo, redo and save", edit_and_save)
            report.check("close and remove the tome", close_and_remove)
    finally:
        for name in ("session", "importer", "library"):
            thing = state.get(name)
            if thing is not None:
                try:
                    thing.close()
                except Exception:
                    pass
