"""Milestone 0: is PyMuPDF's journal reliable enough to be the undo stack?

Each operation runs against a freshly opened copy so one failure cannot cascade.
Usage: python spikes/m0_journal.py <work_dir> <pdf>
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pymupdf

if os.environ.get("BW_PATCH"):
    # Work around PyMuPDF's broken operation guard (see findings in README).
    pymupdf.JM_have_operation = lambda pdf: 1
    if os.environ["BW_PATCH"] == "noextra":
        pymupdf.g_use_extra = False

NO_IMG = pymupdf.PDF_REDACT_IMAGE_NONE
NO_ART = pymupdf.PDF_REDACT_LINE_ART_NONE


def first_text_line(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if len(text) > 8:
                return pymupdf.Rect(line["bbox"]), text
    return None, None


def run(copy, name, make):
    """make(doc) -> (do, is_done, is_undone). Reports each phase separately."""
    doc = pymupdf.open(copy)
    doc.journal_enable()
    phases = []
    try:
        do, is_done, is_undone = make(doc)
        doc.journal_start_op(name)
        do()
        doc.journal_stop_op()
        phases.append(("do", is_done()))
        doc.journal_undo()
        phases.append(("undo", is_undone()))
        doc.journal_redo()
        phases.append(("redo", is_done()))
        doc.journal_undo()
        phases.append(("undo2", is_undone()))
        # a second, different op afterwards must still work
        doc.journal_start_op("follow-up")
        doc[0].add_rect_annot(pymupdf.Rect(10, 10, 40, 40))
        doc.journal_stop_op()
        doc.journal_undo()
        phases.append(("follow-up", True))
        verdict = "PASS" if all(ok for _, ok in phases) else "FAIL"
        print(f"  {verdict:5} {name}: " + " ".join(f"{p}={ok}" for p, ok in phases))
    except Exception as exc:
        done = " ".join(f"{p}={ok}" for p, ok in phases)
        print(f"  ERROR {name}: after [{done}] -> {type(exc).__name__}: {exc}")
    finally:
        doc.close()


def main():
    work, src = Path(sys.argv[1]), sys.argv[2]
    work.mkdir(parents=True, exist_ok=True)
    copy = work / "journal_copy.pdf"
    shutil.copyfile(src, copy)
    probe = pymupdf.open(copy)
    n = probe.page_count
    pno = next(i for i in range(min(5, n - 1), n) if first_text_line(probe[i])[0] is not None)
    rect, text = first_text_line(probe[pno])
    img_page = next((i for i in range(n) if probe[i].get_images()), None)
    probe.close()
    print(f"pymupdf {pymupdf.__version__}; text page {pno + 1}, image page "
          f"{None if img_page is None else img_page + 1}")

    def annots(doc, i=pno):
        return len(list(doc[i].annots() or []))

    def ptext(doc, i):
        return doc[i].get_text("text")

    def highlight(doc):
        base = annots(doc)
        return (lambda: doc[pno].add_highlight_annot(rect),
                lambda: annots(doc) == base + 1, lambda: annots(doc) == base)

    def ink(doc):
        base = annots(doc)
        return (lambda: doc[pno].add_ink_annot([[(50, 50), (80, 90), (120, 60)]]),
                lambda: annots(doc) == base + 1, lambda: annots(doc) == base)

    def freetext(doc):
        base = annots(doc)
        return (lambda: doc[pno].add_freetext_annot(pymupdf.Rect(60, 60, 260, 120), "Note"),
                lambda: annots(doc) == base + 1, lambda: annots(doc) == base)

    def redact(doc):
        before = ptext(doc, pno)
        def do():
            p = doc[pno]
            p.add_redact_annot(rect, fill=False)
            p.apply_redactions(images=NO_IMG, graphics=NO_ART)
        return do, lambda: text not in ptext(doc, pno), lambda: ptext(doc, pno) == before

    def insert_text(doc):
        before = ptext(doc, pno)
        return (lambda: doc[pno].insert_text((72, 72), "BOOKWYRM-PROBE", fontsize=12),
                lambda: "BOOKWYRM-PROBE" in ptext(doc, pno), lambda: ptext(doc, pno) == before)

    def insert_textbox(doc):
        before = ptext(doc, pno)
        box = pymupdf.Rect(72, 72, 300, 140)
        return (lambda: doc[pno].insert_textbox(box, "BOOKWYRM-BOX", fontsize=12),
                lambda: "BOOKWYRM-BOX" in ptext(doc, pno), lambda: ptext(doc, pno) == before)

    def insert_htmlbox(doc):
        before = ptext(doc, pno)
        box = pymupdf.Rect(72, 72, 300, 140)
        return (lambda: doc[pno].insert_htmlbox(box, "<b>BOOKWYRM-HTML</b>"),
                lambda: "BOOKWYRM-HTML" in ptext(doc, pno), lambda: ptext(doc, pno) == before)

    def insert_image(doc):
        base = len(doc[pno].get_images())
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
        pix.clear_with(120)
        return (lambda: doc[pno].insert_image(pymupdf.Rect(50, 50, 150, 150), pixmap=pix),
                lambda: len(doc[pno].get_images()) == base + 1,
                lambda: len(doc[pno].get_images()) == base)

    def move_page(doc):
        a, b = ptext(doc, 0), ptext(doc, 1)
        return (lambda: doc.move_page(1, 0),
                lambda: ptext(doc, 0) == b and ptext(doc, 1) == a,
                lambda: ptext(doc, 0) == a and ptext(doc, 1) == b)

    def delete_page(doc):
        t2 = ptext(doc, 2)
        return (lambda: doc.delete_page(2), lambda: doc.page_count == n - 1,
                lambda: doc.page_count == n and ptext(doc, 2) == t2)

    def rotate(doc):
        r = doc[1].rotation
        return (lambda: doc[1].set_rotation((r + 90) % 360),
                lambda: doc[1].rotation == (r + 90) % 360, lambda: doc[1].rotation == r)

    def insert_pdf(doc):
        def do():
            other = pymupdf.open()
            other.new_page()
            doc.insert_pdf(other)
            other.close()
        return do, lambda: doc.page_count == n + 1, lambda: doc.page_count == n

    def new_page(doc):
        b = ptext(doc, 1)
        return (lambda: doc.new_page(pno=1), lambda: doc.page_count == n + 1,
                lambda: doc.page_count == n and ptext(doc, 1) == b)

    def set_toc(doc):
        toc = doc.get_toc()
        return (lambda: doc.set_toc([[1, "Probe", 1]]),
                lambda: doc.get_toc() == [[1, "Probe", 1]], lambda: doc.get_toc() == toc)

    def set_metadata(doc):
        title = doc.metadata.get("title")
        def do():
            meta = dict(doc.metadata)
            meta["title"] = "Probe Title"
            doc.set_metadata(meta)
        def fresh_title():
            doc.init_doc()
            return doc.metadata.get("title")
        return do, lambda: fresh_title() == "Probe Title", lambda: fresh_title() == title

    def cropbox(doc):
        old = pymupdf.Rect(doc[1].cropbox)
        new = pymupdf.Rect(old.x0 + 20, old.y0 + 20, old.x1 - 20, old.y1 - 20)
        return (lambda: doc[1].set_cropbox(new),
                lambda: abs(doc[1].cropbox.width - new.width) < 1,
                lambda: abs(doc[1].cropbox.width - old.width) < 1)

    for name, make in [
        ("highlight", highlight), ("ink", ink), ("free text", freetext),
        ("redact text", redact), ("insert_text", insert_text),
        ("insert_textbox", insert_textbox), ("insert_htmlbox", insert_htmlbox),
        ("insert_image", insert_image), ("move_page", move_page),
        ("delete_page", delete_page), ("rotate", rotate), ("insert_pdf", insert_pdf),
        ("new_page", new_page), ("set_toc", set_toc), ("set_metadata", set_metadata),
        ("set_cropbox", cropbox),
    ]:
        run(copy, name, make)

    print("\n== saving under journalling")
    doc = pymupdf.open(copy)
    doc.journal_enable()
    doc.journal_start_op("a")
    doc[pno].add_highlight_annot(rect)
    doc.journal_stop_op()
    for label, fn in [
        ("can_save_incrementally", doc.can_save_incrementally),
        ("full save to new file", lambda: doc.save(work / "j_out.pdf", garbage=0) or "ok"),
        ("journal_can_do after save", doc.journal_can_do),
        ("undo after save", lambda: doc.journal_undo() or f"ok, annots={annots(doc)}"),
        ("redo after save", lambda: doc.journal_redo() or f"ok, annots={annots(doc)}"),
        ("saveIncr", lambda: doc.saveIncr() or "ok"),
    ]:
        try:
            print(f"  {label}: {fn()}")
        except Exception as exc:
            print(f"  {label}: ERROR {type(exc).__name__}: {exc}")
    doc.close()
    for f in (copy, work / "j_out.pdf"):
        f.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
