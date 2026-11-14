import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf  # noqa: E402
import pytest  # noqa: E402


def make_pdf(path, pages=6, width=400, height=600, title="Test Tome"):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((50, 80), f"Page {i + 1} of the test tome", fontsize=18)
        page.insert_text((50, 110), f"keyword-{i + 1} dragons hoard silver", fontsize=11)
        page.draw_rect(pymupdf.Rect(50, 140, width - 50, 320), color=(0.2, 0.3, 0.6),
                       fill=(0.7, 0.8, 0.95))
    if pages >= 4:
        doc.set_toc([[1, "First", 1], [1, "Second", 3], [2, "Second-A", 4]])
    doc.set_metadata({"title": title, "author": "A. Wyrm"})
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def sample_pdf(tmp_path):
    return make_pdf(tmp_path / "sample.pdf")
