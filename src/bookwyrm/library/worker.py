"""Library worker process: the slow parts of adding a tome.

Hashing, copying, cover rendering and text extraction all happen here so the
window stays responsive. Like the document host it never imports Qt.

Requests
    ("inspect", job, path)                      -> ("inspected", job, facts)
    ("ingest", job, src, dest_pdf, cover_path)  -> ("ingested", job)
    ("index", job, pdf_path)                    -> ("text", job, [(page, text), ...])...
                                                   ("indexed", job, page_count)
    ("cancel", job)
    ("close",)
Any job may instead answer ("job_failed", job, message).

Indexing a big tome takes several seconds, so it is advanced one batch at a
time and steps aside whenever a quicker request is waiting.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections import deque
from pathlib import Path

from bookwyrm.pdf import repair

BATCH = 20  # pages of text per message
COVER_WIDTH = 480

_PLACEHOLDER_TITLE = re.compile(r"^(untitled|document\d*|\s*)$|\.(pdf|indd|docx?)$", re.I)


def clean_title(metadata_title: str, filename: str) -> str:
    """A presentable title: the PDF's own if it has a real one, else from the file name."""
    title = (metadata_title or "").strip()
    if title and not _PLACEHOLDER_TITLE.search(title):
        return title
    stem = Path(filename).stem
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)  # "(1)" download suffixes
    stem = re.sub(r"\s+", " ", stem).strip(" -_")
    return stem or "Untitled tome"


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(pymupdf, path: str) -> dict:
    doc = pymupdf.open(path)
    try:
        if not doc.is_pdf:
            raise ValueError("not a PDF")
        meta = doc.metadata or {}
        return {
            "sha256": sha256_of(path),
            "file_size": os.path.getsize(path),
            "page_count": doc.page_count,
            "title": clean_title(meta.get("title") or "", path),
            "author": (meta.get("author") or "").strip(),
            "locked": bool(doc.needs_pass),
        }
    finally:
        doc.close()


def ingest(pymupdf, src: str, dest_pdf: str, cover_path: str) -> None:
    dest = Path(dest_pdf)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial")
    shutil.copyfile(src, partial)
    os.replace(partial, dest)
    make_cover(pymupdf, dest_pdf, cover_path)


def make_cover(pymupdf, pdf_path: str, cover_path: str) -> bool:
    """Render page one as the tome's cover. Locked or empty tomes get none."""
    doc = pymupdf.open(pdf_path)
    try:
        if doc.needs_pass or doc.page_count == 0:
            return False
        repair.mend_page(doc, 0)  # in memory only: the file is not written to
        page = doc[0]
        scale = COVER_WIDTH / max(1.0, page.rect.width)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        Path(cover_path).parent.mkdir(parents=True, exist_ok=True)
        pix.save(cover_path, jpg_quality=86)
        return True
    finally:
        doc.close()


def page_texts(pymupdf, pdf_path: str):
    """Yield (page_count, [(page, text), ...]) batches in reading order.

    Ligatures are expanded and line-end hyphens joined so that searching for
    "fire" finds "ﬁre" and "empowered" finds "empow-/ered".
    """
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP \
        | pymupdf.TEXT_DEHYPHENATE
    doc = pymupdf.open(pdf_path)
    try:
        if doc.needs_pass:
            yield 0, []
            return
        count = doc.page_count
        batch = []
        for number in range(count):
            try:
                text = doc[number].get_text("text", flags=flags)
            except Exception:
                text = ""
            batch.append((number, text))
            if len(batch) >= BATCH:
                yield count, batch
                batch = []
        yield count, batch
    finally:
        doc.close()


def serve(conn) -> None:
    import pymupdf

    from bookwyrm.pdf import compat

    compat.apply()
    quick: deque = deque()  # inspect / ingest requests
    slow: deque = deque()  # index requests waiting their turn
    cancelled: set[int] = set()
    running = None  # (job, generator) of the index in progress

    def pump(block: bool) -> bool:
        """Read waiting requests. Returns False when asked to close."""
        while block or conn.poll():
            block = False
            msg = conn.recv()
            op = msg[0]
            if op == "close":
                return False
            if op == "cancel":
                cancelled.add(msg[1])
            elif op == "index":
                slow.append(msg)
            else:
                quick.append(msg)
        return True

    while True:
        idle = not quick and not slow and running is None
        if not pump(block=idle):
            return
        if quick:
            msg = quick.popleft()
            op, job = msg[0], msg[1]
            if job in cancelled:
                continue
            try:
                if op == "inspect":
                    conn.send(("inspected", job, inspect(pymupdf, msg[2])))
                elif op == "ingest":
                    ingest(pymupdf, msg[2], msg[3], msg[4])
                    conn.send(("ingested", job))
            except Exception as exc:
                conn.send(("job_failed", job, f"{type(exc).__name__}: {exc}"))
            continue
        if running is None and slow:
            msg = slow.popleft()
            if msg[1] not in cancelled:
                running = (msg[1], page_texts(pymupdf, msg[2]))
            continue
        if running is not None:
            job, batches = running
            if job in cancelled:
                batches.close()
                running = None
                continue
            try:
                count, batch = next(batches)
                if batch:
                    conn.send(("text", job, batch))
                if not batch or batch[-1][0] >= count - 1:
                    batches.close()
                    running = None
                    conn.send(("indexed", job, count))
            except StopIteration:
                running = None
                conn.send(("indexed", job, 0))
            except Exception as exc:
                running = None
                conn.send(("job_failed", job, f"{type(exc).__name__}: {exc}"))


def run(pipe: int) -> int:
    """Entry used by ``bookwyrm --worker <pipe number>``."""
    from bookwyrm import channel

    conn = channel.connect(pipe)
    try:
        serve(conn)
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        conn.close()
    return 0
