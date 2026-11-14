"""The Scriptorium's engine: every change to a tome, with undo, redo and save.

Runs inside the document host. Each edit is one MuPDF journal operation, so
undo and redo restore the document exactly, back to the last save. Alongside
the journal this keeps a log of what each operation touched (which pages need
repainting, whether the page order changed) and where every page came from in
the last-saved file, so bookmarks and pins can follow their pages on save.

Qt-free. Operations are plain functions ``op(editor, **args)`` registered in
``OPERATIONS``; they return an ``Outcome``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field

import pymupdf

from bookwyrm.pdf import compat

OPERATIONS: dict = {}


def operation(name: str, label: str):
    """Register an edit. `label` is what Undo/Redo call it."""
    def register(fn):
        OPERATIONS[name] = (fn, label)
        return fn
    return register


@dataclass
class Outcome:
    pages: list[int] | None = field(default_factory=list)  # pages to repaint; None = all
    structural: bool = False  # page count, order or size changed
    result: object = None  # returned to the caller (e.g. a new annotation's xref)


class EditError(Exception):
    """An edit that cannot be done, phrased for the user."""


def replace_file(source: str, target: str, attempts: int = 12, pause: float = 0.15) -> None:
    """Move `source` over `target`, waiting out a brief lock on `target`.

    Windows refuses to replace a file another program has open, and virus
    scanners and search indexers open files for a moment right after they change.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class Editor:
    def __init__(self, doc: pymupdf.Document, path: str, password: str | None = None):
        compat.apply()
        self.doc = doc
        self.path = path
        self.password = password
        self.stranded: str | None = None  # a save written to disk but not yet swapped in
        self._reset()

    def _reset(self) -> None:
        self.journalling = False
        self.epoch = 0  # bumped by every undo/redo; see font()
        self.log: list[dict] = []  # every operation still reachable by undo or redo
        self.position = 0  # how many of them are currently applied
        self.origins: list[int | None] = list(range(self.doc.page_count))

    # -- state ----------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """True when the tome in memory differs from the library file on disk."""
        return self.position > 0 or self.stranded is not None

    def _open(self, path: str) -> None:
        self.doc = pymupdf.open(path)
        if self.doc.needs_pass and self.password is not None:
            self.doc.authenticate(self.password)

    def can_undo(self) -> bool:
        return self.position > 0

    def can_redo(self) -> bool:
        return self.position < len(self.log)

    def summary(self) -> dict:
        return {
            "dirty": self.dirty,
            "can_undo": self.can_undo(),
            "can_redo": self.can_redo(),
            "undo_name": self.log[self.position - 1]["label"] if self.can_undo() else "",
            "redo_name": self.log[self.position]["label"] if self.can_redo() else "",
        }

    # -- doing, undoing, redoing --------------------------------------------------

    def apply(self, name: str, args: dict) -> dict:
        """Run a registered operation as one undoable step. Returns its log entry."""
        if name not in OPERATIONS:
            raise EditError(f"Unknown edit “{name}”.")
        if not self.doc.is_pdf:
            raise EditError("Only PDF tomes can be edited.")
        fn, label = OPERATIONS[name]
        if not self.journalling:
            self._prewarm()
            self.doc.journal_enable()
            self.journalling = True
        before = list(self.origins)
        mark = self.doc.journal_position()[0]
        self.doc.journal_start_op(label)
        try:
            outcome = fn(self, **args)
        except Exception:
            self._abandon(mark)
            self.origins = before
            raise
        self.doc.journal_stop_op()
        del self.log[self.position:]
        entry = {"label": label, "pages": outcome.pages, "structural": outcome.structural,
                 "before": before, "after": list(self.origins), "result": outcome.result}
        self.log.append(entry)
        self.position += 1
        return entry

    def _prewarm(self) -> None:
        """Create, before history starts, the font the engine uses for text boxes.

        MuPDF remembers shared resources (fonts, colour spaces, images) it has added
        to a document and reuses them by object number. That memory cannot be
        cleared, and undo removes the objects, so a text box added after an undo
        would point at a font that no longer exists. A font created *before*
        journalling begins is never undone, so the engine's memory of it stays true.
        The stray annotation is deleted at once; the unreferenced font is dropped
        when the tome is next saved.
        """
        try:
            page = self.doc[0]
            annot = page.add_freetext_annot(pymupdf.Rect(0, 0, 60, 20), "x", fontsize=8,
                                            fontname="helv")
            annot.update()
            page.delete_annot(annot)
        except Exception:
            pass  # e.g. not a PDF; apply() reports that properly

    def font(self, source) -> pymupdf.Font:
        """A font safe to embed now: a built-in code ('helv', 'tiro', ...) or font file bytes.

        For the same reason as _prewarm(), a font embedded after an undo must not be
        recognised as one embedded before it. A few trailing bytes that differ per
        undo make the engine treat it as new; font parsers ignore them.
        """
        data = pymupdf.Font(source).buffer if isinstance(source, str) else source
        return pymupdf.Font(fontbuffer=bytes(data) + b"\x00BW%d" % self.epoch)

    def _abandon(self, mark: int) -> None:
        """Roll back a half-done operation.

        An operation that failed before changing anything leaves no journal
        entry, so only undo when the journal actually advanced past `mark`.
        """
        try:
            self.doc.journal_stop_op()
            if self.doc.journal_position()[0] > mark:
                self.doc.journal_undo()
        except Exception:
            pass
        self._refresh_caches()

    def undo(self) -> dict | None:
        if not self.can_undo():
            return None
        entry = self.log[self.position - 1]
        self.doc.journal_undo()
        self.position -= 1
        self.origins = list(entry["before"])
        self._refresh_caches()
        return entry

    def redo(self) -> dict | None:
        if not self.can_redo():
            return None
        entry = self.log[self.position]
        self.doc.journal_redo()
        self.position += 1
        self.origins = list(entry["after"])
        self._refresh_caches()
        return entry

    def _refresh_caches(self) -> None:
        """Drop what PyMuPDF remembers in Python; undo and redo happen beneath it.

        Besides the outline and metadata, PyMuPDF remembers which images and fonts
        it has already inserted (by object number) so it can reuse them. After an
        undo those numbers may be gone or belong to something else, and reusing
        them corrupts the page.
        """
        doc = self.doc
        self.epoch += 1
        for name in ("InsertedImages", "FontInfos", "Graftmaps", "ShownPages"):
            cache = getattr(doc, name, None)
            if hasattr(cache, "clear"):
                cache.clear()
        try:
            doc.init_doc()
        except Exception:
            pass

    # -- saving ---------------------------------------------------------------------

    def save(self, archive_path: str | None = None) -> dict:
        """Write the tome to disk, keeping a pristine copy the first time.

        The new file is written beside the old one and swapped in, so a crash
        mid-save cannot leave a half-written tome. Saving commits the changes
        inside the PDF engine, after which its journal can no longer take them
        back; the tome is therefore reopened from the saved file and the undo
        history starts afresh.

        If the library file cannot be replaced (on Windows: another program has
        it open), nothing is lost: the tome carries on from the newly written
        file, stays marked as unsaved, and the next save tries again.
        """
        if archive_path and not os.path.exists(archive_path):
            os.makedirs(os.path.dirname(archive_path), exist_ok=True)
            shutil.copyfile(self.path, archive_path)
        temporary = self.path + ".saving"
        if self.stranded == temporary:  # an earlier save is still waiting under that name
            temporary = self.path + ".saving2"
        # garbage=1 drops objects nothing refers to any more (replaced pictures, a
        # superseded outline). Safe here because the tome is reopened straight after.
        self.doc.save(temporary, garbage=1, encryption=pymupdf.PDF_ENCRYPT_KEEP)

        count = self.doc.page_count
        mapping = None
        if self.origins != list(range(count)):
            mapping = {origin: index for index, origin in enumerate(self.origins)
                       if origin is not None}
        origins = list(self.origins)
        self.doc.close()  # Windows will not replace a file that is still open
        previous, self.stranded = self.stranded, None
        try:
            replace_file(temporary, self.path)
        except OSError as problem:
            self._open(temporary)  # carry on from what was just written
            self._reset()
            self.origins = origins  # still measured against the file in the library
            self.stranded = temporary
            self._discard(previous)
            raise EditError(
                "The tome could not be replaced in the library because another program "
                f"is using the file ({problem}). Your changes are safe; close the other "
                "program and save again.") from problem
        self._discard(previous)
        self._open(self.path)
        self._reset()
        return {
            "file_size": os.path.getsize(self.path),
            "sha256": sha256_of(self.path),
            "page_count": count,
            "mapping": mapping,  # old page -> new page for surviving pages; None = unchanged
        }

    @staticmethod
    def _discard(path: str | None) -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def restore(self, archive_path: str) -> dict:
        """Put the archive copy (the tome as first imported) back in place of the current file."""
        if not os.path.exists(archive_path):
            raise EditError("There is no archive copy of this tome to restore.")
        temporary = self.path + ".restoring"
        shutil.copyfile(archive_path, temporary)
        current, origins = self.stranded or self.path, list(self.origins)
        self.doc.close()
        try:
            replace_file(temporary, self.path)
        except OSError as problem:
            self._open(current)
            self._reset()
            if self.stranded is not None:
                self.origins = origins
            self._discard(temporary)
            raise EditError(
                "The original could not be restored because another program is using the "
                f"tome's file ({problem}). Close the other program and try again.") from problem
        self._discard(self.stranded)
        self.stranded = None
        self._open(self.path)
        self._reset()
        return {
            "file_size": os.path.getsize(self.path),
            "sha256": sha256_of(self.path),
            "page_count": self.doc.page_count,
            "mapping": None,
            "restored": True,
        }

    # -- helpers for operations --------------------------------------------------------

    def page(self, number: int):
        if not 0 <= number < self.doc.page_count:
            raise EditError(f"There is no page {number + 1}.")
        return self.doc[number]


# ======================================================================================
# Annotations
# ======================================================================================

def _color(value, default=(1.0, 0.85, 0.2)):
    if not value:
        return default
    return tuple(float(c) for c in value[:3])


def _finish(annot, color=None, width=None, opacity=None, fill=None, author="Bookwyrm"):
    if color is not None or fill is not None:
        annot.set_colors(stroke=color, fill=fill)
    if width is not None:
        annot.set_border(width=float(width))
    if opacity is not None:
        annot.set_opacity(float(opacity))
    annot.set_info(title=author)
    annot.update()
    return annot.xref


MARKUP = {"highlight": "add_highlight_annot", "underline": "add_underline_annot",
          "strikeout": "add_strikeout_annot", "squiggly": "add_squiggly_annot"}


@operation("markup", "Mark Text")
def op_markup(editor: Editor, page: int, kind: str, rects: list, color=None) -> Outcome:
    """Highlight, underline or strike through text. `rects` are line boxes in page points."""
    if kind not in MARKUP:
        raise EditError(f"Unknown markup “{kind}”.")
    target = editor.page(page)
    quads = [pymupdf.Rect(*r).quad for r in rects]
    if not quads:
        raise EditError("Select some text first.")
    default = (1.0, 0.85, 0.2) if kind == "highlight" else (0.85, 0.15, 0.15)
    annot = getattr(target, MARKUP[kind])(quads=quads)
    return Outcome([page], result=_finish(annot, _color(color, default)))


@operation("note", "Add Note")
def op_note(editor: Editor, page: int, point, text: str, color=None) -> Outcome:
    annot = editor.page(page).add_text_annot(pymupdf.Point(*point), text, icon="Note")
    return Outcome([page], result=_finish(annot, _color(color, (1.0, 0.82, 0.3))))


@operation("textbox", "Add Text Box")
def op_textbox(editor: Editor, page: int, rect, text: str, fontsize: float = 11,
               color=None, fill=None) -> Outcome:
    annot = editor.page(page).add_freetext_annot(
        pymupdf.Rect(*rect), text, fontsize=float(fontsize), fontname="helv",
        text_color=_color(color, (0.1, 0.1, 0.1)), fill_color=fill and _color(fill))
    annot.set_info(title="Bookwyrm")
    annot.update()
    return Outcome([page], result=annot.xref)


@operation("ink", "Draw")
def op_ink(editor: Editor, page: int, strokes: list, color=None, width: float = 2.0) -> Outcome:
    strokes = [[(float(x), float(y)) for x, y in stroke] for stroke in strokes
               if len(stroke) >= 2]
    if not strokes:
        raise EditError("Nothing was drawn.")
    annot = editor.page(page).add_ink_annot(strokes)
    return Outcome([page], result=_finish(annot, _color(color, (0.8, 0.1, 0.1)), width))


@operation("shape", "Add Shape")
def op_shape(editor: Editor, page: int, kind: str, points, color=None, width: float = 1.5,
             fill=None) -> Outcome:
    """kind: rect | ellipse | line | arrow. `points` is (x0, y0, x1, y1)."""
    target = editor.page(page)
    x0, y0, x1, y1 = (float(v) for v in points)
    stroke = _color(color, (0.8, 0.1, 0.1))
    if kind in ("rect", "ellipse"):
        box = pymupdf.Rect(x0, y0, x1, y1).normalize()
        annot = target.add_rect_annot(box) if kind == "rect" else target.add_circle_annot(box)
        return Outcome([page], result=_finish(annot, stroke, width, fill=fill and _color(fill)))
    if kind in ("line", "arrow"):
        annot = target.add_line_annot(pymupdf.Point(x0, y0), pymupdf.Point(x1, y1))
        if kind == "arrow":
            annot.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
            return Outcome([page], result=_finish(annot, stroke, width, fill=stroke))
        return Outcome([page], result=_finish(annot, stroke, width))
    raise EditError(f"Unknown shape “{kind}”.")


def _annot(editor: Editor, page: int, xref: int):
    target = editor.page(page)
    for annot in target.annots() or ():
        if annot.xref == xref:
            return target, annot
    raise EditError("That annotation is no longer there.")


@operation("delete_annot", "Delete Annotation")
def op_delete_annot(editor: Editor, page: int, xref: int) -> Outcome:
    target, annot = _annot(editor, page, xref)
    target.delete_annot(annot)
    return Outcome([page])


@operation("move_annot", "Move Annotation")
def op_move_annot(editor: Editor, page: int, xref: int, rect) -> Outcome:
    _, annot = _annot(editor, page, xref)
    annot.set_rect(pymupdf.Rect(*rect))
    annot.update()
    return Outcome([page])


@operation("edit_annot", "Edit Annotation")
def op_edit_annot(editor: Editor, page: int, xref: int, text: str | None = None,
                  color=None, width=None) -> Outcome:
    _, annot = _annot(editor, page, xref)
    if text is not None:
        annot.set_info(content=text)
    if color is not None:
        annot.set_colors(stroke=_color(color))
    if width is not None:
        annot.set_border(width=float(width))
    annot.update()
    return Outcome([page])


def list_annots(doc, page: int) -> list[dict]:
    """What is drawn on top of a page, for selecting and editing."""
    found = []
    for annot in doc[page].annots() or ():
        kind = annot.type[1]
        if kind in ("Widget", "Link", "Popup"):
            continue
        rect = annot.rect
        found.append({
            "xref": annot.xref, "kind": kind,
            "rect": (rect.x0, rect.y0, rect.x1, rect.y1),
            "text": (annot.info or {}).get("content", ""),
            "movable": kind in ("Text", "FreeText", "Square", "Circle", "Stamp"),
        })
    return found


# ======================================================================================
# Forms
# ======================================================================================

def list_widgets(doc, page: int) -> list[dict]:
    """The fillable fields on a page."""
    fields = []
    for widget in doc[page].widgets() or ():
        rect = widget.rect
        kind = widget.field_type_string
        entry = {
            "xref": widget.xref, "kind": kind, "name": widget.field_name or "",
            "rect": (rect.x0, rect.y0, rect.x1, rect.y1),
            "value": widget.field_value,
            "read_only": bool(widget.field_flags & pymupdf.PDF_FIELD_IS_READ_ONLY),
            "multiline": bool(widget.field_flags & pymupdf.PDF_TX_FIELD_IS_MULTILINE),
            "choices": list(widget.choice_values or []) if kind in ("ComboBox", "ListBox")
            else [],
            "calculated": bool(getattr(widget, "script_calc", None)),
        }
        if kind in ("CheckBox", "RadioButton"):
            try:
                entry["on_state"] = widget.on_state()
            except Exception:
                entry["on_state"] = "Yes"
        fields.append(entry)
    return fields


@operation("fill_field", "Fill Field")
def op_fill_field(editor: Editor, page: int, xref: int, value) -> Outcome:
    for widget in editor.page(page).widgets() or ():
        if widget.xref == xref:
            if widget.field_flags & pymupdf.PDF_FIELD_IS_READ_ONLY:
                raise EditError("That field is read-only.")
            widget.field_value = value
            widget.update()
            return Outcome([page])
    raise EditError("That field is no longer there.")


# ======================================================================================
# Pages
# ======================================================================================

def _check_pages(editor: Editor, pages) -> list[int]:
    count = editor.doc.page_count
    pages = [int(p) for p in pages]
    if not pages:
        raise EditError("Choose at least one page.")
    for page in pages:
        if not 0 <= page < count:
            raise EditError(f"There is no page {page + 1}.")
    return pages


@operation("reorder_pages", "Rearrange Pages")
def op_reorder_pages(editor: Editor, order: list) -> Outcome:
    """Keep exactly the pages in `order` (current page numbers), in that order.

    Covers moving pages and deleting them in one step; `order` may not repeat a page.
    """
    order = _check_pages(editor, order)
    if len(set(order)) != len(order):
        raise EditError("A page cannot appear twice.")
    if order == list(range(editor.doc.page_count)):
        raise EditError("The pages are already in that order.")
    editor.doc.select(order)
    editor.origins = [editor.origins[page] for page in order]
    return Outcome(None, structural=True)


@operation("delete_pages", "Delete Pages")
def op_delete_pages(editor: Editor, pages: list) -> Outcome:
    doomed = set(_check_pages(editor, pages))
    count = editor.doc.page_count
    if len(doomed) >= count:
        raise EditError("A tome needs at least one page.")
    editor.doc.delete_pages(sorted(doomed))
    editor.origins = [o for page, o in enumerate(editor.origins) if page not in doomed]
    return Outcome(None, structural=True)


@operation("rotate_pages", "Rotate Pages")
def op_rotate_pages(editor: Editor, pages: list, angle: int) -> Outcome:
    if angle % 90:
        raise EditError("Pages turn in quarter turns.")
    for number in _check_pages(editor, pages):
        page = editor.doc[number]
        page.set_rotation((page.rotation + angle) % 360)
    return Outcome(None, structural=True)


@operation("insert_blank", "Insert Blank Page")
def op_insert_blank(editor: Editor, at: int, width: float = 0, height: float = 0) -> Outcome:
    """Insert an empty page before page `at` (or at the end). Size defaults to a neighbour's."""
    doc = editor.doc
    at = max(0, min(int(at), doc.page_count))
    if width <= 0 or height <= 0:
        model = doc[min(at, doc.page_count - 1)].rect
        width, height = model.width, model.height
    with compat.python_paths():
        doc.new_page(pno=at if at < doc.page_count else -1, width=width, height=height)
    editor.origins.insert(at, None)
    return Outcome(None, structural=True, result=at)


@operation("insert_pdf", "Insert Pages")
def op_insert_pdf(editor: Editor, path: str, at: int, first: int = 0,
                  last: int | None = None) -> Outcome:
    """Insert pages `first`..`last` of another PDF before page `at`."""
    doc = editor.doc
    at = max(0, min(int(at), doc.page_count))
    if os.path.abspath(path) == os.path.abspath(editor.path):
        raise EditError("A tome cannot be inserted into itself.")
    try:
        source = pymupdf.open(path)
    except Exception as exc:
        raise EditError("That file could not be read as a PDF.") from exc
    try:
        if not source.is_pdf or source.needs_pass:
            raise EditError("That file is not a PDF Bookwyrm can read.")
        last = source.page_count - 1 if last is None else min(int(last), source.page_count - 1)
        first = max(0, int(first))
        if first > last:
            raise EditError("That PDF has no such pages.")
        with compat.python_paths():
            doc.insert_pdf(source, from_page=first, to_page=last,
                           start_at=at if at < doc.page_count else -1)
    finally:
        source.close()
    added = last - first + 1
    editor.origins[at:at] = [None] * added
    return Outcome(None, structural=True, result=added)


@operation("crop_page", "Crop Page")
def op_crop_page(editor: Editor, page: int, rect) -> Outcome:
    """Crop to `rect`, given in the page's displayed coordinates."""
    target = editor.page(page)
    box = pymupdf.Rect(*rect).normalize()
    if box.width < 20 or box.height < 20:
        raise EditError("That crop is too small.")
    # Displayed coordinates are rotated and start at the crop box corner; the PDF
    # stores the crop box unrotated, measured from the media box corner.
    box = box * target.derotation_matrix
    offset = target.cropbox_position
    box = (box + (offset.x, offset.y, offset.x, offset.y)) & target.mediabox
    target.set_cropbox(box)
    return Outcome(None, structural=True)


# ======================================================================================
# Contents (outline) and properties
# ======================================================================================

@operation("set_toc", "Edit Contents")
def op_set_toc(editor: Editor, toc: list) -> Outcome:
    """toc: [[level, title, 1-based page], ...]."""
    cleaned, previous = [], 0
    count = editor.doc.page_count
    for level, title, page, *_rest in toc:
        level, page, title = int(level), int(page), str(title).strip()
        if not title:
            raise EditError("Every contents entry needs a title.")
        if level < 1 or level > previous + 1:
            raise EditError(
                f"“{title}” is indented too far: an entry can sit at most one level "
                "below the entry before it.")
        if not 1 <= page <= count:
            raise EditError(f"“{title}” points to page {page}, which does not exist.")
        cleaned.append([level, title, page])
        previous = level
    doc = editor.doc
    # set_toc() deletes the old outline objects and reuses their numbers, and the
    # journal does not record deletions, so undo would bring back an empty outline.
    # Detaching the old outline first leaves its objects untouched for undo to
    # reconnect; the orphans are dropped the next time the tome is compacted.
    doc.xref_set_key(doc.pdf_catalog(), "Outlines", "null")
    doc.set_toc(cleaned)
    return Outcome([])


@operation("set_metadata", "Edit Properties")
def op_set_metadata(editor: Editor, fields: dict) -> Outcome:
    allowed = ("title", "author", "subject", "keywords")
    meta = dict(editor.doc.metadata or {})
    for key in allowed:
        if key in fields:
            meta[key] = str(fields[key]).strip()
    editor.doc.set_metadata(meta)
    return Outcome([])


def extract_pages(doc, pages, destination: str) -> int:
    """Write the given pages of `doc` to a new PDF at `destination`. Not an edit."""
    pages = [int(p) for p in pages if 0 <= int(p) < doc.page_count]
    if not pages:
        raise EditError("Choose at least one page.")
    new = pymupdf.open()
    try:
        for page in pages:
            new.insert_pdf(doc, from_page=page, to_page=page)
        new.save(destination, garbage=3, deflate=True)
    finally:
        new.close()
    return len(pages)


# ======================================================================================
# Page content: text, pictures, erasing
# ======================================================================================

# Text-span style bits, as PyMuPDF reports them.
ITALIC, SERIF, MONO, BOLD = 2, 4, 8, 16
_BUILTIN = {  # (monospaced, serifed, bold, italic) -> built-in font code
    (False, False, False, False): "helv", (False, False, True, False): "hebo",
    (False, False, False, True): "heit", (False, False, True, True): "hebi",
    (False, True, False, False): "tiro", (False, True, True, False): "tibo",
    (False, True, False, True): "tiit", (False, True, True, True): "tibi",
    (True, False, False, False): "cour", (True, False, True, False): "cobo",
    (True, False, False, True): "coit", (True, False, True, True): "cobi",
}


def list_text_lines(doc, page: int) -> list[dict]:
    """Every line of real text on a page, with the style needed to re-set it."""
    lines = []
    flags = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE \
        | pymupdf.TEXT_MEDIABOX_CLIP
    for block in doc[page].get_text("dict", flags=flags)["blocks"]:
        for line in block.get("lines", ()):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            lead = max(spans, key=lambda s: len(s["text"]))  # the style most of the line uses
            x0, y0, x1, y1 = line["bbox"]
            color = lead["color"]
            lines.append({
                "rect": (x0, y0, x1, y1),
                "text": "".join(s["text"] for s in line["spans"]).rstrip(),
                "font": lead["font"], "size": lead["size"], "flags": lead["flags"],
                "color": ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255,
                          (color & 255) / 255),
                "origin": tuple(spans[0]["origin"]),
                "upright": abs(line["dir"][0] - 1.0) < 0.01 and abs(line["dir"][1]) < 0.01,
            })
    return lines


def _font_for(editor: Editor, page, name: str, flags: int, text: str):
    """The tome's own font if it can spell `text`, otherwise the closest built-in one.

    PDFs usually embed only the letters they already use, so the original font is
    often unable to write new words. Returns (Font, used_original).
    """
    doc = editor.doc
    needed = {ord(c) for c in text if not c.isspace()}
    wanted = name.split("+")[-1]
    for xref, _ext, _kind, basefont, *_rest in page.get_fonts(full=True):
        if basefont.split("+")[-1] != wanted:
            continue
        try:
            buffer = doc.extract_font(xref)[3]
            if not buffer:
                continue
            font = editor.font(buffer)
            if all(font.has_glyph(code) for code in needed):
                return font, True
        except Exception:
            continue  # not a usable stand-alone font file
    key = (bool(flags & MONO), bool(flags & SERIF) and not flags & MONO,
           bool(flags & BOLD), bool(flags & ITALIC))
    return editor.font(_BUILTIN[key]), False


def _remove_text(page, rect) -> None:
    """Take the text out of `rect` and leave pictures and line art exactly as they are."""
    page.add_redact_annot(rect, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


@operation("replace_text", "Edit Text")
def op_replace_text(editor: Editor, page: int, rect, text: str, font: str = "",
                    size: float = 11, flags: int = 0, color=None, origin=None) -> Outcome:
    """Replace one line of text. The new text starts where the old line started."""
    target = editor.page(page)
    box = pymupdf.Rect(*rect)
    if box.is_empty:
        raise EditError("There is no text there to edit.")
    # Trim the box a little so the lines above and below are not caught.
    trim = box.height * 0.12
    _remove_text(target, pymupdf.Rect(box.x0, box.y0 + trim, box.x1, box.y1 - trim))
    used_original = True
    if text.strip():
        target = editor.page(page)  # applying redactions rewrote the page
        face, used_original = _font_for(editor, target, font, flags, text)
        start = pymupdf.Point(*origin) if origin else pymupdf.Point(box.x0, box.y1 - size * 0.2)
        writer = pymupdf.TextWriter(target.rect, color=_color(color, (0, 0, 0)))
        writer.append(start, text, font=face, fontsize=float(size))
        writer.write_text(target)
    return Outcome([page], result={"original_font": used_original})


@operation("add_text", "Add Text")
def op_add_text(editor: Editor, page: int, point, text: str, size: float = 11,
                color=None, serif: bool = True) -> Outcome:
    """Write new text; `point` is the start of the first line's baseline."""
    if not text.strip():
        raise EditError("There is nothing to add.")
    target = editor.page(page)
    face = editor.font("tiro" if serif else "helv")
    writer = pymupdf.TextWriter(target.rect, color=_color(color, (0, 0, 0)))
    x, y = float(point[0]), float(point[1])
    for number, line in enumerate(text.splitlines()):
        if line:
            writer.append(pymupdf.Point(x, y + number * size * 1.25), line, font=face,
                          fontsize=float(size))
    writer.write_text(target)
    return Outcome([page])


@operation("erase", "Erase")
def op_erase(editor: Editor, page: int, rect, mode: str = "text") -> Outcome:
    """Clear a region. mode: 'text' (text and line art), 'all' (pictures too), 'cover' (white)."""
    target = editor.page(page)
    box = pymupdf.Rect(*rect).normalize()
    if mode == "cover":
        target.draw_rect(box, color=None, fill=(1, 1, 1), overlay=True)
        return Outcome([page])
    if mode not in ("text", "all"):
        raise EditError(f"Unknown way to erase: “{mode}”.")
    target.add_redact_annot(box, fill=False, cross_out=False)
    target.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_PIXELS if mode == "all" else pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    return Outcome([page])


def list_images(doc, page: int) -> list[dict]:
    """Pictures placed on a page: where they are and which stored image each one shows."""
    found = []
    for info in doc[page].get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        if info.get("xref", 0) <= 0 or x1 - x0 < 2 or y1 - y0 < 2:
            continue  # inline images have no number and cannot be edited
        found.append({"xref": info["xref"], "rect": (x0, y0, x1, y1),
                      "width": info["width"], "height": info["height"]})
    return found


def image_uses(doc, xref: int) -> int:
    """How many pages show the stored image `xref`."""
    return sum(1 for number in range(doc.page_count)
               if any(entry[0] == xref for entry in doc.get_page_images(number)))


def _new_object(doc, definition: str, stream: bytes | None = None,
                already_encoded: str | None = None) -> int:
    """Create an object. `already_encoded` names the filter of pre-compressed stream data."""
    xref = doc.get_new_xref()
    doc.update_object(xref, definition)
    if stream is not None:
        doc.update_stream(xref, stream, new=True, compress=already_encoded is None)
        if already_encoded:  # storing raw bytes drops /Filter; say what they are
            doc.xref_set_key(xref, "Filter", already_encoded)
    return xref


def _embed_picture(doc, path: str) -> int:
    """Store a picture file in the tome and return its object number.

    The image object is written by hand rather than through MuPDF's image
    insertion, which remembers images by content and would hand back the number
    of one that undo has since removed.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    pix = pymupdf.Pixmap(data)
    if pix.width < 1 or pix.height < 1:
        raise EditError("That picture is empty.")
    size = f"/Width {pix.width}/Height {pix.height}/BitsPerComponent 8"
    if data[:3] == b"\xff\xd8\xff" and not pix.alpha and pix.colorspace is not None \
            and pix.colorspace.n in (1, 3):
        space = "/DeviceGray" if pix.colorspace.n == 1 else "/DeviceRGB"
        return _new_object(  # a JPEG goes in as it is, without re-encoding
            doc, f"<</Type/XObject/Subtype/Image{size}/ColorSpace{space}>>", data,
            already_encoded="/DCTDecode")
    mask = ""
    if pix.alpha:
        alpha = pymupdf.Pixmap(None, pix)  # the transparency channel on its own
        mask_xref = _new_object(
            doc, f"<</Type/XObject/Subtype/Image{size}/ColorSpace/DeviceGray>>", alpha.samples)
        mask = f"/SMask {mask_xref} 0 R"
        pix = pymupdf.Pixmap(pix, 0)  # colour without alpha
    if pix.colorspace is None or pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    space = "/DeviceGray" if pix.colorspace.n == 1 else "/DeviceRGB"
    return _new_object(
        doc, f"<</Type/XObject/Subtype/Image{size}/ColorSpace{space}{mask}>>", pix.samples)


def _rewrite(doc, xref: int, entries: dict, stream: bytes, raw: bool) -> None:
    """Give an existing stream object new contents and a new dictionary, in place.

    The object is changed key by key rather than replaced wholesale: the journal
    restores a stream correctly on undo only when the object itself stays put.
    """
    doc.update_stream(xref, stream, new=True, compress=not raw)
    for key in doc.xref_get_keys(xref):
        if key != "Length" and key not in entries and not (key == "Filter" and not raw):
            doc.xref_set_key(xref, key, "null")
    for key, value in entries.items():
        doc.xref_set_key(xref, key, value)


def _copy_object(doc, source: int, target: int) -> None:
    """Make stream object `target` an exact copy of `source`."""
    entries = {key: doc.xref_get_key(source, key)[1] for key in doc.xref_get_keys(source)
               if key != "Length"}
    _rewrite(doc, target, entries, doc.xref_stream_raw(source), raw=True)


def _blank_picture(doc, xref: int) -> None:
    """Turn a stored image into something that draws nothing, wherever it is shown."""
    _rewrite(doc, xref, {"Type": "/XObject", "Subtype": "/Form", "BBox": "[0 0 1 1]"},
             b" ", raw=True)


def _is_picture(doc, xref: int) -> bool:
    try:
        return doc.xref_is_image(xref)
    except Exception:
        return False


@operation("delete_image", "Delete Picture")
def op_delete_image(editor: Editor, page: int, xref: int) -> Outcome:
    """Blank a stored image. It disappears from every page that shows it."""
    editor.page(page)
    if not _is_picture(editor.doc, xref):
        raise EditError("That picture is no longer there.")
    _blank_picture(editor.doc, xref)
    return Outcome(None)


@operation("move_image", "Move Picture")
def op_move_image(editor: Editor, page: int, xref: int, rect) -> Outcome:
    """Show the picture at `rect` instead (moving and resizing are the same thing).

    A copy is placed at the new spot and the stored original is blanked, so any
    other page showing the same stored image loses it too.
    """
    target = editor.page(page)
    doc = editor.doc
    if not _is_picture(doc, xref):
        raise EditError("That picture is no longer there.")
    copy = doc.get_new_xref()
    doc.update_object(copy, "<<>>")
    _copy_object(doc, xref, copy)
    target.insert_image(pymupdf.Rect(*rect).normalize(), xref=copy, keep_proportion=False)
    _blank_picture(doc, xref)
    return Outcome(None)


@operation("replace_image", "Replace Picture")
def op_replace_image(editor: Editor, page: int, xref: int, path: str) -> Outcome:
    editor.page(page)
    doc = editor.doc
    if not os.path.exists(path):
        raise EditError("That picture file could not be found.")
    if not _is_picture(doc, xref):
        raise EditError("That picture is no longer there.")
    try:
        new = _embed_picture(doc, path)
    except EditError:
        raise
    except Exception as exc:
        raise EditError("That file could not be read as a picture.") from exc
    _copy_object(doc, new, xref)
    return Outcome(None)


@operation("insert_image", "Add Picture")
def op_insert_image(editor: Editor, page: int, rect, path: str) -> Outcome:
    target = editor.page(page)
    if not os.path.exists(path):
        raise EditError("That picture file could not be found.")
    try:
        xref = _embed_picture(editor.doc, path)
    except EditError:
        raise
    except Exception as exc:
        raise EditError("That file could not be read as a picture.") from exc
    target.insert_image(pymupdf.Rect(*rect).normalize(), xref=xref, keep_proportion=True)
    return Outcome([page])


# ======================================================================================
# Reading aids: links and printed page numbers (nothing here changes the tome)
# ======================================================================================

def list_links(doc, page: int) -> list[dict]:
    """Clickable areas on a page: jumps inside the tome, and web addresses.

    Each entry has "rect" and either "page" (0-based) with "y" (how far down that
    page to land, in points) or "uri". Links to other files are left out.
    """
    count = doc.page_count
    found = []
    for link in doc[page].get_links():
        rect = link.get("from")
        if rect is None or rect.is_empty:
            continue
        entry = {"rect": (rect.x0, rect.y0, rect.x1, rect.y1)}
        kind = link.get("kind")
        target = link.get("page", -1)
        if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and isinstance(target, int) \
                and 0 <= target < count:
            spot = link.get("to")
            entry["page"] = target
            entry["y"] = max(0.0, float(spot.y)) if hasattr(spot, "y") else 0.0
        elif kind == pymupdf.LINK_URI and link.get("uri"):
            entry["uri"] = str(link["uri"])
        else:
            continue
        found.append(entry)
    return found


def page_labels(doc) -> list[str] | None:
    """The page number printed on each page ("iv", "86", "A-3"), if the tome records them.

    PDFs can say that, for instance, the seventh sheet is page "1". Returns one label
    per page, or None when the tome carries no such numbering.
    """
    try:
        if not doc.get_page_labels():
            return None
        return [doc[number].get_label() or str(number + 1) for number in range(doc.page_count)]
    except Exception:
        return None
