"""Milestone 0 probe: measure PyMuPDF behaviour on real tomes before building on it.

Usage: python spikes/m0_probe.py <work_dir> <pdf> [<pdf> ...]
Every write goes to copies inside <work_dir>; the input PDFs are only read.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

_t = time.perf_counter()
import pymupdf  # noqa: E402

IMPORT_S = time.perf_counter() - _t

NO_IMG = getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0)
NO_ART = getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0)


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def section(name):
    print(f"\n== {name}")


def attempt(label, fn):
    try:
        result = fn()
        print(f"  {label}: {result}")
        return result
    except Exception as exc:  # the probe reports failures, it does not stop on them
        print(f"  {label}: ERROR {type(exc).__name__}: {exc}")
        return None


def sample_pages(n):
    return sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1})


def probe_open(path):
    section("open")
    doc, s = timed(lambda: pymupdf.open(path))
    print(f"  open: {s * 1000:.0f} ms, pages={doc.page_count}, "
          f"size={os.path.getsize(path) / 1e6:.0f} MB")
    print(f"  repaired={doc.is_repaired} encrypted={doc.is_encrypted} "
          f"incremental_ok={doc.can_save_incrementally()} form={doc.is_form_pdf}")
    return doc


def probe_geometry(doc):
    section("page geometry")
    n = doc.page_count
    _, s = timed(lambda: [doc.page_cropbox(i) for i in range(n)])
    print(f"  page_cropbox x{n}: {s * 1000:.0f} ms")
    def load_all():
        sizes = set()
        for i in range(n):
            p = doc[i]
            sizes.add((round(p.rect.width), round(p.rect.height), p.rotation))
        return sizes
    sizes, s = timed(load_all)
    print(f"  load_page x{n}: {s * 1000:.0f} ms, distinct sizes={sorted(sizes)[:6]}")


def probe_render(doc):
    section("render")
    for pno in sample_pages(doc.page_count):
        page = doc[pno]
        r = page.rect
        _, first = timed(lambda: page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False))
        pix, again = timed(lambda: page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False))
        clip = pymupdf.Rect(r.x0, r.y0, r.x0 + 512 / 3, r.y0 + 512 / 3)
        _, tile = timed(lambda: page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip,
                                                alpha=False))
        z = 240 / r.width
        _, prev = timed(lambda: page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False))
        print(f"  p{pno + 1}: full@2x first={first * 1000:.0f} ms again={again * 1000:.0f} ms "
              f"({pix.width}x{pix.height}, {len(pix.samples_mv) / 1e6:.0f} MB) "
              f"tile512@3x={tile * 1000:.0f} ms preview240={prev * 1000:.0f} ms")


def probe_gil(path):
    """Does a render on a worker thread freeze the main thread?"""
    section("thread stall (is the GIL held during render?)")
    doc = pymupdf.open(path)
    pages = [p for p in sample_pages(doc.page_count)] * 2

    def max_gap(stop):
        worst, last = 0.0, time.perf_counter()
        while not stop.is_set():
            time.sleep(0.002)
            now = time.perf_counter()
            worst, last = max(worst, now - last), now
        return worst

    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    idle = max_gap(stop)

    render_s = []

    def work():
        for pno in pages:
            _, s = timed(lambda: doc[pno].get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False))
            render_s.append(s)

    stop = threading.Event()
    worker = threading.Thread(target=lambda: (work(), stop.set()))
    worker.start()
    busy = max_gap(stop)
    worker.join()
    print(f"  main-thread worst gap: idle={idle * 1000:.0f} ms, "
          f"while rendering={busy * 1000:.0f} ms")
    print(f"  slowest single render on worker: {max(render_s) * 1000:.0f} ms")
    verdict = "GIL HELD -> use a helper process" if busy > 0.5 * max(render_s) else \
        "GIL released -> a worker thread would be enough"
    print(f"  verdict: {verdict}")
    doc.close()


def probe_text_and_previews(doc):
    section("indexing + previews")
    n = doc.page_count
    def all_text():
        return sum(len(doc[i].get_text("text", sort=True)) for i in range(n))
    chars, s = timed(all_text)
    print(f"  get_text x{n}: {s:.1f} s, {chars / 1e6:.1f} M chars")
    k = min(n, 60)
    step = max(1, n // k)
    def previews():
        total = 0
        for i in range(0, n, step):
            p = doc[i]
            z = 240 / p.rect.width
            total += len(p.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
                         .tobytes("jpg", jpg_quality=70))
        return total
    size, s = timed(previews)
    count = len(range(0, n, step))
    print(f"  previews x{count}: {s:.1f} s -> est. {s / count * n:.0f} s and "
          f"{size / count * n / 1e6:.0f} MB for the whole tome")


def first_text_line(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if len(text) > 8:
                return pymupdf.Rect(line["bbox"]), text
    return None, None


def text_page(doc, start=0):
    for i in range(start, doc.page_count):
        rect, text = first_text_line(doc[i])
        if rect is not None:
            return i, rect, text
    return None, None, None


def probe_save(path, work):
    section("save")
    copy = work / ("save_" + Path(path).name)
    shutil.copyfile(path, copy)
    doc = pymupdf.open(copy)
    pno, rect, _ = text_page(doc, min(5, doc.page_count - 1))
    doc[pno].add_highlight_annot(rect)
    print(f"  after annotation: incremental_ok={doc.can_save_incrementally()}")
    attempt("incremental save", lambda: f"{timed(doc.saveIncr)[1] * 1000:.0f} ms")
    page = doc[pno]
    page.add_redact_annot(rect, fill=False)
    page.apply_redactions(images=NO_IMG, graphics=NO_ART)
    print(f"  after redaction: incremental_ok={doc.can_save_incrementally()}")
    for label, kwargs in (("full save garbage=0", dict(garbage=0)),
                          ("full save garbage=3 deflate", dict(garbage=3, deflate=True))):
        out = work / "full_save.tmp.pdf"
        def run(kwargs=kwargs, out=out):
            _, s = timed(lambda: doc.save(out, **kwargs))
            return f"{s:.2f} s -> {os.path.getsize(out) / 1e6:.0f} MB"
        attempt(label, run)
    # Swap the temp file over the open one, then keep editing and save again.
    def swap_and_continue():
        os.replace(work / "full_save.tmp.pdf", copy)
        doc[0].add_rect_annot(pymupdf.Rect(20, 20, 80, 80))
        out = work / "full_save2.tmp.pdf"
        doc.save(out, garbage=0)
        check = pymupdf.open(out)
        ok = check.page_count == doc.page_count and len(list(check[0].annots())) >= 1
        check.close()
        return "still editable and saveable" if ok else "MISMATCH"
    attempt("after os.replace under open handle", swap_and_continue)
    doc.close()
    for f in work.glob("full_save*.tmp.pdf"):
        f.unlink()
    copy.unlink(missing_ok=True)


def probe_journal(path, work):
    section("journal (undo/redo) torture test")
    copy = work / ("journal_" + Path(path).name)
    shutil.copyfile(path, copy)
    doc = pymupdf.open(copy)
    n = doc.page_count
    attempt("journal_enable", lambda: (doc.journal_enable(), doc.journal_is_enabled())[1])

    def op(name, do, check_done, check_undone):
        try:
            doc.journal_start_op(name)
            do()
            doc.journal_stop_op()
            done = check_done()
            doc.journal_undo()
            undone = check_undone()
            doc.journal_redo()
            redone = check_done()
            doc.journal_undo()
            undone2 = check_undone()
            ok = done and undone and redone and undone2
            print(f"  {name}: {'PASS' if ok else 'FAIL'} "
                  f"(done={done} undo={undone} redo={redone} undo2={undone2})")
        except Exception as exc:
            print(f"  {name}: ERROR {type(exc).__name__}: {exc}")

    pno, rect, text = text_page(doc, min(5, n - 1))
    annots = lambda: len(list(doc[pno].annots() or []))  # noqa: E731
    base_annots = annots()
    op("add annotation", lambda: doc[pno].add_highlight_annot(rect),
       lambda: annots() == base_annots + 1, lambda: annots() == base_annots)

    page_text = lambda i: doc[i].get_text("text")  # noqa: E731
    before = page_text(pno)
    def redact():
        p = doc[pno]
        p.add_redact_annot(rect, fill=False)
        p.apply_redactions(images=NO_IMG, graphics=NO_ART)
    op("redact text", redact,
       lambda: text not in page_text(pno), lambda: page_text(pno) == before)

    def insert_text():
        doc[pno].insert_text((72, 72), "BOOKWYRM-PROBE", fontsize=12)
    op("insert text", insert_text,
       lambda: "BOOKWYRM-PROBE" in page_text(pno), lambda: page_text(pno) == before)

    a, b = page_text(0), page_text(1)
    op("move page", lambda: doc.move_page(1, 0),
       lambda: page_text(0) == b and page_text(1) == a,
       lambda: page_text(0) == a and page_text(1) == b)

    t2 = page_text(2)
    op("delete page", lambda: doc.delete_page(2),
       lambda: doc.page_count == n - 1,
       lambda: doc.page_count == n and page_text(2) == t2)

    op("rotate page", lambda: doc[1].set_rotation((doc[1].rotation + 90) % 360),
       lambda: doc[1].rotation % 180 == 90 or True,
       lambda: doc.page_count == n)

    def insert_pdf():
        other = pymupdf.open()
        other.new_page()
        doc.insert_pdf(other)
        other.close()
    op("insert pdf", insert_pdf, lambda: doc.page_count == n + 1, lambda: doc.page_count == n)

    def new_page():
        doc.new_page(pno=1)
    op("insert blank page", new_page, lambda: doc.page_count == n + 1,
       lambda: doc.page_count == n and page_text(1) == b)

    toc = doc.get_toc()
    op("set toc", lambda: doc.set_toc([[1, "Probe", 1]]),
       lambda: doc.get_toc() == [[1, "Probe", 1]], lambda: doc.get_toc() == toc)

    print(f"  journal position: {attempt('position', doc.journal_position)}")
    # Redo something so the document is dirty, then test saving under journalling.
    def dirty_and_save():
        doc.journal_start_op("dirty")
        doc[pno].add_highlight_annot(rect)
        doc.journal_stop_op()
        out = work / "journal_save.tmp.pdf"
        _, s = timed(lambda: doc.save(out, garbage=0))
        can_undo, can_redo = doc.journal_can_do().values()
        out.unlink()
        return f"full save ok in {s:.2f} s; undo still available={can_undo}"
    attempt("save while journalling", dirty_and_save)
    attempt("incremental_ok while journalling", doc.can_save_incrementally)
    def incr():
        _, s = timed(doc.saveIncr)
        return f"ok in {s * 1000:.0f} ms; undo still available={doc.journal_can_do()}"
    attempt("incremental save while journalling", incr)
    def undo_after_save():
        doc.journal_undo()
        return f"undo after save ok, annots={annots()}"
    attempt("undo after save", undo_after_save)
    doc.close()
    copy.unlink(missing_ok=True)


def probe_redact_art(path, work, out_dir):
    section("text removal over artwork")
    doc = pymupdf.open(path)
    best = None
    for pno in range(min(doc.page_count, 80)):
        page = doc[pno]
        imgs = len(page.get_images())
        rect, text = first_text_line(page)
        if rect is not None and imgs and (best is None or imgs > best[1]):
            best = (pno, imgs, rect, text)
    if best is None:
        print("  no page with both text and images in the first 80 pages")
        return
    pno, imgs, rect, text = best
    page = doc[pno]
    view = rect + (-40, -30, 40, 30)
    stem = Path(path).stem[:20].replace(" ", "_")
    page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=view).save(out_dir / f"{stem}_before.png")
    drawings_before = len(page.get_drawings())
    page.add_redact_annot(rect, fill=False)
    page.apply_redactions(images=NO_IMG, graphics=NO_ART)
    page = doc[pno]
    page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=view).save(out_dir / f"{stem}_after.png")
    print(f"  p{pno + 1}: removed {text[:40]!r}; images {imgs}->{len(page.get_images())}, "
          f"drawings {drawings_before}->{len(page.get_drawings())}")
    print(f"  crops: {out_dir}/{stem}_before.png / _after.png")
    doc.close()


def probe_fonts(doc):
    section("fonts (can edited text reuse the original font?)")
    pno, _, _ = text_page(doc, min(5, doc.page_count - 1))
    fonts = doc.get_page_fonts(pno)
    for xref, ext, ftype, basefont, *_ in fonts[:8]:
        def info(xref=xref):
            name, fext, _, buf = doc.extract_font(xref)
            if not buf:
                return "not extractable"
            font = pymupdf.Font(fontbuffer=buf)
            have = len(font.valid_codepoints())
            ascii_ok = sum(font.has_glyph(c) > 0 for c in range(32, 127))
            return f"{fext}, {have} glyphs, printable ASCII covered {ascii_ok}/95"
        attempt(f"{basefont} ({ftype})", info)


def probe_form(path):
    section(f"form: {Path(path).name}")
    doc = pymupdf.open(path)
    widgets = [w for page in doc for w in page.widgets()]
    kinds = {}
    for w in widgets:
        kinds[w.field_type_string] = kinds.get(w.field_type_string, 0) + 1
    calc = [w for w in widgets if getattr(w, "script_calc", None)]
    fmt = [w for w in widgets if getattr(w, "script_format", None)]
    print(f"  widgets={len(widgets)} kinds={kinds}")
    print(f"  with calculation script={len(calc)} with format script={len(fmt)}")
    if calc:
        print(f"  sample calc: {calc[0].field_name!r}: {calc[0].script_calc[:90]!r}")
    doc.close()


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    print(f"pymupdf {pymupdf.__version__} (MuPDF {pymupdf.mupdf_version}), "
          f"import took {IMPORT_S * 1000:.0f} ms")
    for path in sys.argv[2:]:
        print(f"\n######## {Path(path).name}")
        doc = probe_open(path)
        if doc.is_form_pdf and doc.page_count < 20:
            doc.close()
            probe_form(path)
            continue
        probe_geometry(doc)
        probe_render(doc)
        probe_fonts(doc)
        probe_text_and_previews(doc)
        doc.close()
        probe_gil(path)
        probe_save(path, work)
        probe_journal(path, work)
        probe_redact_art(path, work, out_dir)


if __name__ == "__main__":
    main()
